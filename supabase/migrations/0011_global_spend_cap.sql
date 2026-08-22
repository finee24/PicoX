-- =============================================================================
-- 0011 — Tetto globale di spesa giornaliera
-- =============================================================================
--
-- IL VETTORE (voce A4 di SECURITY_AUDIT.md)
--
-- I tre limiti gia' in vigore — creator attivi (`0004`/`0007`), analisi
-- giornaliere (`0008`), validazioni giornaliere (`0010`) — sono tutti **per
-- utente**. Chi registra N account ottiene N volte ogni tetto, e il costo
-- torna a scalare linearmente: le email usa e getta sono gratuite e
-- automatizzabili, quindi la barriera di oggi alza il costo dell'attacco ma
-- non lo chiude.
--
-- L'audit elenca quattro risposte possibili (CAPTCHA al signup, limite di
-- registrazioni per IP o dominio, verifica della carta sul piano gratuito,
-- tetto globale di spesa). Questa migration implementa la quarta, che l'audit
-- indica come **l'unica che protegge anche dagli scenari non previsti**: non
-- distingue un attaccante da un difetto nostro che genera lavoro a vuoto, e
-- non dipende dal numero di account.
--
-- NON SOSTITUISCE I TETTI PER PIANO, LI COMPLETA
--
-- Sono due domande diverse. «Questo utente ha esaurito cio' che il suo piano
-- gli concede?» resta la domanda dei trigger PX001/PX002/PX003. «Il servizio
-- nel suo insieme ha speso troppo oggi?» e' la domanda di questo. Il secondo
-- e' una rete di sicurezza, non una quota: quando scatta, qualcosa e' gia'
-- andato storto — un abuso distribuito, un bug che riaccoda analisi, un
-- creator che pubblica a raffica.
--
-- PERCHE' UNA TABELLA E NON UNA COSTANTE
--
-- Il tetto e i costi unitari cambiano quando cambiano i listini dei provider o
-- il modello Gemini in uso. Una costante nel codice richiederebbe un deploy per
-- alzare il tetto **mentre il servizio e' fermo perche' il tetto e' basso**:
-- esattamente il momento in cui un deploy e' l'ultima cosa che si vuole fare.
-- Una riga in tabella si cambia con un UPDATE dalla console.
--
-- I COSTI UNITARI NON SONO INVENTATI QUI
--
-- Vengono dai due documenti che li avevano gia' derivati, e restano derivati da
-- listini pubblici e non misurati sul consumo reale, come i tetti per piano:
--
--   cost_per_analysis_usd   ~$0,035  (`0008`: ~$0,032 Gemini + ~$0,0027 Apify)
--   cost_per_validation_usd ~$0,003  (`0010`: Apify per un profilo IG/TikTok)
--
-- `daily_cap_usd` a 100.00 e' invece una **decisione**, non una derivazione:
-- e' ~2857 analisi o ~33.000 validazioni al giorno sull'intero servizio, cioe'
-- circa 95 utenti `free` che esauriscono la loro quota di analisi nello stesso
-- giorno. Molto sopra l'uso legittimo previsto oggi, molto sotto la spesa che
-- un abuso automatizzato produrrebbe in poche ore ($37-$175 all'ora per singolo
-- account, misurato nella voce A3).
--
-- E' LA MISURA CHE OGGI MANCA (voce B4)
--
-- `estimated_spend_today_usd()` e' il primo punto del sistema che dice quanto
-- si sta spendendo, invece di dedurlo. Il backend lo registra in un WARNING
-- strutturato quando il tetto interviene: quel log e' la base per un alert
-- vero, che non e' di questo giro.


-- -----------------------------------------------------------------------------
-- 1. I parametri, in una riga sola
-- -----------------------------------------------------------------------------
-- Riga singola imposta dal database, non dalla convenzione: `id boolean primary
-- key default true check (id)` ammette esattamente il valore `true`, quindi un
-- secondo INSERT collide sulla primary key. Senza, due righe di configurazione
-- silenziosamente divergenti sarebbero a un INSERT di distanza, e il trigger ne
-- leggerebbe una a caso.

create table if not exists public.spend_limits (
  id                      boolean     primary key default true check (id),
  daily_cap_usd           numeric(12, 4) not null check (daily_cap_usd > 0),
  cost_per_analysis_usd   numeric(12, 6) not null check (cost_per_analysis_usd >= 0),
  cost_per_validation_usd numeric(12, 6) not null check (cost_per_validation_usd >= 0),
  updated_at              timestamptz not null default now()
);

comment on table public.spend_limits is
  'Riga unica con il tetto di spesa giornaliero dell''intero servizio e i costi '
  'unitari con cui la spesa viene stimata. In tabella e non nel codice perche'' '
  'alzare il tetto deve essere un UPDATE, non un deploy: il momento in cui '
  'serve alzarlo e'' quello in cui il servizio e'' fermo.';

comment on column public.spend_limits.daily_cap_usd is
  'Decisione, non derivazione: ~2857 analisi al giorno sull''intero servizio. '
  'Sopra l''uso legittimo previsto, sotto la spesa che un abuso automatizzato '
  'produce in poche ore (voce A3: $37-$175/ora per singolo account).';

comment on column public.spend_limits.cost_per_analysis_usd is
  'Da `0008`: ~$0,032 di inferenza Gemini su 30s al tier caro piu'' ~$0,0027 di '
  'Apify per risolvere l''URL. Derivato da listini pubblici, non misurato.';

comment on column public.spend_limits.cost_per_validation_usd is
  'Da `0010`: ~$0,003 di Apify per un profilo Instagram o TikTok. Per YouTube '
  'il costo monetario e'' zero ma consuma 1 unita'' della quota giornaliera '
  'globale della Data API, che questa colonna non modella.';

-- `on conflict do nothing` e non `do update`: rieseguire la migration non deve
-- riportare ai valori di partenza un tetto alzato a mano durante un incidente.
insert into public.spend_limits (
  id, daily_cap_usd, cost_per_analysis_usd, cost_per_validation_usd
)
values (true, 100.00, 0.035, 0.003)
on conflict (id) do nothing;


-- -----------------------------------------------------------------------------
-- 2. La spesa stimata di oggi, su entrambe le tabelle insieme
-- -----------------------------------------------------------------------------
-- Una funzione sola e non due conteggi copiati nei due trigger: e' la stessa
-- regola di `creator_limit_for_tier` e sorelle — i numeri e le formule stanno
-- in un posto solo, perche' due copie divergono alla prima modifica.
--
-- `>= date_trunc('day', now())` e non `created_at::date = current_date`: la
-- seconda forma non e' sargable e ignorerebbe `analysis_events_user_created_idx`
-- e `validation_events_user_created_idx`, trasformando due conteggi su poche
-- righe in due scansioni complete.
--
-- ATTENZIONE AL CONFINE DEL GIORNO. `date_trunc('day', <timestamptz>)` tronca
-- rispetto al **TimeZone della sessione**, non a UTC: e' UTC solo perche' le
-- sessioni di questo progetto girano con `TimeZone = UTC`, che e' il default di
-- Supabase e non un vincolo espresso da questo SQL. La forma e' identica a
-- quella gia' usata da `enforce_analysis_quota` (`0008`) e
-- `enforce_validation_quota` (`0010`), quindi i quattro limiti condividono
-- **lo stesso** confine di giornata: e' la proprieta' che conta, ed e' il
-- motivo per cui qui non si diverge dalle due migration precedenti. Se un
-- giorno si vorra' ancorare il confine a UTC nel codice invece che nella
-- configurazione, va fatto sulle tre insieme — cambiarne una sola creerebbe due
-- "giorni" diversi nello stesso database.

create or replace function public.estimated_spend_today_usd()
returns numeric
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select count(*) from public.analysis_events a
      where a.created_at >= date_trunc('day', now())) * l.cost_per_analysis_usd
    + (select count(*) from public.validation_events v
        where v.created_at >= date_trunc('day', now())) * l.cost_per_validation_usd,
    0
  )
  from public.spend_limits l
  where l.id;
$$;

comment on function public.estimated_spend_today_usd() is
  'Spesa stimata dell''intero servizio nel giorno UTC corrente: righe di oggi '
  'in analysis_events e validation_events, ciascuna moltiplicata per il proprio '
  'costo unitario e sommate. Stima, non fattura: i costi unitari sono derivati '
  'da listini pubblici. E'' il dato che la voce B4 dell''audit chiede e che oggi '
  'non esiste da nessun''altra parte.';

revoke execute on function public.estimated_spend_today_usd() from public;


-- -----------------------------------------------------------------------------
-- 3. Il trigger, condiviso dalle due tabelle
-- -----------------------------------------------------------------------------
-- Una funzione sola per due tabelle: `TG_TABLE_NAME` dice quale costo unitario
-- applicare alla riga in arrivo. Due funzioni gemelle divergerebbero, ed e' la
-- stessa ragione per cui i tetti per piano stanno in funzioni SQL uniche.
--
-- LA CONDIZIONE E' `>` E NON `>=`, DI PROPOSITO.
--
-- Si rifiuta la riga che **supera** il tetto, non quella che lo raggiunge
-- esattamente. Con `>=` il tetto sarebbe di fatto un'unita' piu' basso di
-- quanto dichiara, e "tetto di 100 dollari" significherebbe "fino a 99,965".
--
-- SE `spend_limits` E' VUOTA, NON SI BLOCCA NULLA.
--
-- Il meccanismo e' la guardia esplicita `if tetto is null or costo_riga is
-- null then return new`, qui sotto: senza riga di configurazione il `select
-- ... into` non trova nulla e lascia entrambe le variabili a `null`, quindi si
-- esce prima ancora di calcolare la spesa. E' deliberato — una tabella di
-- configurazione svuotata per errore deve degradare verso "nessun tetto", non
-- verso "servizio fermo". Il rischio speculare (spendere senza rete) e'
-- visibile nei log e nella fattura; un servizio bloccato da una riga mancante
-- sarebbe un'interruzione totale con una causa difficile da trovare.
--
-- La stessa guardia copre un secondo caso, che non e' un errore di
-- configurazione ma di installazione: se questo trigger finisse su una tabella
-- diversa dalle due previste, il `case` su `tg_table_name` non troverebbe un
-- costo unitario e `costo_riga` sarebbe `null`. Anche li' si lascia passare —
-- un trigger installato dove non doveva non deve poter bloccare scritture che
-- nessuno ha mai inteso limitare.

create or replace function public.enforce_global_spend_cap()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  tetto         numeric;
  spesa_oggi    numeric;
  costo_riga    numeric;
  spesa_futura  numeric;
begin
  select l.daily_cap_usd,
         case tg_table_name
           when 'analysis_events'   then l.cost_per_analysis_usd
           when 'validation_events' then l.cost_per_validation_usd
         end
    into tetto, costo_riga
    from public.spend_limits l
   where l.id;

  if tetto is null or costo_riga is null then
    return new;
  end if;

  spesa_oggi := public.estimated_spend_today_usd();
  spesa_futura := spesa_oggi + costo_riga;

  if spesa_futura > tetto then
    -- Quarto SQLSTATE di dominio, dopo PX001 (tetto creator), PX002 (quota
    -- analisi) e PX003 (quota validazioni). Distinto dai tre non perche' il
    -- client debba dire all'utente qualcosa di piu' preciso — al contrario:
    -- verso il client questo caso deve restare **generico**, perche' rivelare
    -- che esiste un tetto globale e a quanto sta darebbe a un attaccante la
    -- mappa di dove colpire. Il codice separato serve al **backend**, che deve
    -- poterlo distinguere per registrarlo come incidente invece che come
    -- normale rifiuto di quota.
    --
    -- I numeri stanno in `detail` e non nel messaggio: il backend li registra
    -- nei log, e nulla di tutto cio' raggiunge il client.
    raise exception using
      errcode = 'PX004',
      message = 'Tetto di spesa globale giornaliero raggiunto.',
      detail  = format(
        'spend_today_usd=%s;row_cost_usd=%s;daily_cap_usd=%s;source_table=%s',
        spesa_oggi, costo_riga, tetto, tg_table_name
      );
  end if;

  return new;
end;
$$;

comment on function public.enforce_global_spend_cap() is
  'Rete di sicurezza globale: rifiuta la riga che porterebbe la spesa stimata '
  'del giorno sopra spend_limits.daily_cap_usd, contando analisi e validazioni '
  'insieme. Indipendente dal numero di account, quindi copre il rischio Sybil '
  'della voce A4 che i tetti per piano non chiudono. Degrada verso "nessun '
  'tetto" se spend_limits e'' vuota: un servizio fermo per una riga mancante '
  'sarebbe peggio della spesa che il tetto evita.';

revoke execute on function public.enforce_global_spend_cap() from public;


-- -----------------------------------------------------------------------------
-- 4. L'ORDINE DI ESECUZIONE RISPETTO AI TETTI PER PIANO
-- -----------------------------------------------------------------------------
-- Postgres esegue i trigger BEFORE ROW dello stesso evento **in ordine
-- alfabetico di nome**. Non e' un dettaglio estetico: e' l'unico modo che
-- abbiamo di decidere quale dei due errori arriva all'utente quando una sola
-- richiesta supera entrambi i limiti.
--
-- Deve vincere il piu' specifico. Chi ha esaurito la propria quota giornaliera
-- ha bisogno di sapere quello — e' una condizione che lo riguarda, spiegabile e
-- con un rimedio ("domani", o un piano superiore). "Servizio momentaneamente
-- non disponibile" al posto di quel messaggio manderebbe a cercare un guasto
-- inesistente, ed e' anche informazione peggiore: nasconde che l'utente aveva
-- comunque finito la sua quota.
--
-- I nomi scelti danno quell'ordine da soli, su entrambe le tabelle:
--
--   analysis_events_enforce_quota     < analysis_events_enforce_spend_cap
--   validation_events_enforce_quota   < validation_events_enforce_spend_cap
--
-- ('q' precede 's'). Non c'e' nulla da correggere nelle migration precedenti: i
-- trigger esistenti mantengono i loro nomi e la loro precedenza. Chi
-- rinominasse un giorno questi trigger deve sapere che sta cambiando **quale
-- errore vede l'utente**, non solo un'etichetta.

drop trigger if exists analysis_events_enforce_spend_cap on public.analysis_events;

create trigger analysis_events_enforce_spend_cap
  before insert on public.analysis_events
  for each row
  execute function public.enforce_global_spend_cap();

drop trigger if exists validation_events_enforce_spend_cap on public.validation_events;

create trigger validation_events_enforce_spend_cap
  before insert on public.validation_events
  for each row
  execute function public.enforce_global_spend_cap();


-- -----------------------------------------------------------------------------
-- 5. Privilegi: nessuno per i client
-- -----------------------------------------------------------------------------
-- Stesso trattamento di `analysis_locks`, `job_locks`, `subscriptions`,
-- `analysis_events` e `validation_events`. `revoke all` copre anche TRUNCATE,
-- che non e' soggetto al RLS — la lezione della `0002`.
--
-- Qui la riservatezza conta piu' che altrove: `daily_cap_usd` e la spesa
-- corrente dicono a chi le legge **quanto manca al blocco**. Un attaccante che
-- potesse interrogare questa tabella saprebbe se conviene insistere, e un
-- attaccante che potesse scriverci alzerebbe il tetto invece di aggirarlo.

alter table public.spend_limits enable row level security;

revoke all on public.spend_limits from anon, authenticated;

grant all on public.spend_limits to service_role;


-- -----------------------------------------------------------------------------
-- 6. COSA QUESTO TETTO NON FA
-- -----------------------------------------------------------------------------
-- 1. **Non conta la spesa del cron.** `analysis_events` registra solo il
--    percorso manuale (`conta_quota=False` dal cron, vedi `0008`), quindi il
--    lavoro notturno resta fuori dalla stima. E' coerente con la scelta della
--    `0008` di tenere due budget separati, ma significa che
--    `estimated_spend_today_usd()` sottostima la spesa reale del servizio. Il
--    giorno in cui il cron sara' acceso, questa e' la prima cosa da rivedere.
--
-- 2. **Non conta la quota YouTube.** E' una risorsa scarsa globale (10.000
--    unita' al giorno per progetto) ma non monetaria, e mescolarla a un tetto
--    in dollari darebbe un numero che non significa nulla. Serve un tetto
--    proprio, se e quando la si vorra' sorvegliare.
--
-- 3. **Non e' un alert.** Quando scatta, il servizio e' gia' fermo per tutti.
--    Il WARNING strutturato che il backend registra e' il dato su cui costruire
--    un avviso all'80% del tetto; l'avviso stesso non e' di questo giro.
