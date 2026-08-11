-- =============================================================================
-- 0008 — Quota giornaliera di analisi sul percorso manuale
-- =============================================================================
--
-- IL VETTORE, MISURATO (voce A3 di SECURITY_AUDIT.md)
--
-- `POST /api/v1/analyze-video` non aveva ne' rate limiter ne' quota. Misurato
-- sull'endpoint autenticato: **64 richieste al minuto accettate in sequenza,
-- zero risposte 429, e 10 richieste concorrenti su 10 accettate**. Ogni
-- richiesta accettata e' un'inferenza Gemini piu' una chiamata Apify: con
-- concorrenza 10 e latenza 60s sono ~600 analisi l'ora, cioe' **$37-$175
-- all'ora per singolo account** a seconda del modello dietro
-- `gemini-flash-latest`.
--
-- Il tetto ai creator della `0004` non lo copriva: quello limita il **cron**,
-- questo e' il percorso **manuale**. Due strade diverse verso la stessa spesa,
-- e ne era sorvegliata una sola.
--
-- PERCHE UNA TABELLA APPEND-ONLY E NON UN CONTATORE
--
-- Un contatore aggregato — una riga per (utente, giorno) incrementata a ogni
-- analisi — sarebbe O(1) invece di O(righe del giorno). Ma l'incremento
-- richiede `on conflict do update set conteggio = conteggio + 1`, che un upsert
-- PostgREST **non puo' esprimere**: servirebbe una RPC, cioe' una nuova via
-- d'accesso al database accanto alle due che `supabase_service` dichiara essere
-- le uniche (`service_table` e `unscoped_service_table`).
--
-- Una riga per analisi invece passa dall'`insert` che il backend gia' usa, e in
-- piu' **e' il dato grezzo che serve alla voce B4** dell'audit: misurare i costi
-- reali per utente prima di fissare i prezzi. Oggi quel dato non esiste da
-- nessuna parte, e i numeri qui sotto sono derivati proprio perche' manca.
--
-- Il costo del conteggio e' irrilevante: con un tetto di poche decine al giorno
-- e l'indice qui sotto, il trigger conta pochissime righe.
--
-- QUOTA SOLO SUL PERCORSO MANUALE
--
-- Il cron chiama la stessa `perform_analysis`, ma ha gia' il proprio budget —
-- creator attivi x `apify_results_per_creator`, imposto dalla `0004` e reso
-- retroattivo dalla `0007`. Farlo consumare la quota manuale renderebbe il
-- limite inspiegabile: "perche' non posso analizzare? perche' stanotte e'
-- girato il cron". Due budget indipendenti, ognuno con un proprietario chiaro.
--
-- Lato applicativo la distinzione e' un parametro esplicito
-- (`conta_quota=False` dal cron), non dedotto da `creator_id` o `scraped`, che
-- oggi separano i due chiamanti solo per convenzione.
--
-- I NUMERI SONO SEGNAPOSTO, E LO SONO DICHIARATAMENTE
--
-- Costo per analisi manuale: ~$0,032 di inferenza Gemini su un video di 30s al
-- tier caro, piu' ~$0,0027 di Apify per risolvere l'URL = **~$0,035**.
--
--   free  30 analisi/giorno  ~$1,05/giorno
--   pro  300 analisi/giorno  ~$10,50/giorno
--
-- Come per `creator_limit_for_tier`, sono numeri **derivati da timeout
-- configurati e listini pubblici, non misurati sul consumo reale**. Vanno
-- fissati contro il prezzo del piano, che non esiste ancora — ed e' questa
-- tabella a rendere possibile misurarli.

-- -----------------------------------------------------------------------------
-- 1. Il tetto per piano, accanto al suo gemello
-- -----------------------------------------------------------------------------
-- Stessa forma di `creator_limit_for_tier` (0007) e per la stessa ragione: i
-- numeri devono esistere in un posto solo. Due `CASE` copiati divergono alla
-- prima modifica, ed e' la voce B3 dell'audit.

create or replace function public.analysis_limit_for_tier(tier text)
returns integer
language sql
immutable
set search_path = ''
as $$
  select case coalesce(tier, 'free')
           when 'pro' then 300
           else 30
         end;
$$;

comment on function public.analysis_limit_for_tier(text) is
  'Analisi manuali al giorno ammesse per un dato piano. Numeri segnaposto, '
  'derivati dal costo per analisi (~$0,035) e non misurati: vanno fissati '
  'contro il prezzo del piano quando esistera'', come per creator_limit_for_tier.';

revoke execute on function public.analysis_limit_for_tier(text) from public;


-- -----------------------------------------------------------------------------
-- 2. Una riga per analisi avviata
-- -----------------------------------------------------------------------------

create table if not exists public.analysis_events (
  id          uuid        primary key default gen_random_uuid(),
  user_id     uuid        not null references auth.users (id) on delete cascade,
  -- Denormalizzati per rendere la tabella utile alla misura dei costi (B4)
  -- senza dover ricostruire nulla: `analysis_mode` cambia il costo
  -- dell'inferenza, e `video_url` permette di distinguere un abuso su un solo
  -- video da un uso distribuito.
  video_url   text        not null,
  analysis_mode text      not null check (analysis_mode in ('INFO', 'STYLE', 'BOTH')),
  created_at  timestamptz not null default now()
);

comment on table public.analysis_events is
  'Una riga per analisi manuale **avviata** — non completata. Registra il '
  'momento in cui la spesa e'' decisa, quindi comprende anche le analisi che '
  'pagano Apify e poi falliscono su Gemini. Serve a due cose: imporre la quota '
  'giornaliera, e fornire il dato grezzo per misurare i costi reali per utente.';

comment on column public.analysis_events.created_at is
  'Istante dell''avvio. E'' la colonna su cui si conta la quota del giorno, in '
  'UTC: il "giorno" e'' quello del database, non quello del fuso dell''utente.';

-- L'indice serve al trigger, che a ogni insert conta le righe del giorno per
-- quell'utente. Senza, il costo del controllo crescerebbe con lo storico
-- dell'intera tabella invece che con le righe di oggi.
create index if not exists analysis_events_user_created_idx
  on public.analysis_events (user_id, created_at desc);


-- -----------------------------------------------------------------------------
-- 3. Il trigger che impone la quota
-- -----------------------------------------------------------------------------
-- BEFORE INSERT e non un CHECK: il vincolo dipende da altre righe della stessa
-- tabella e dal piano dell'utente, cioe' da cose che un CHECK non puo' vedere.

create or replace function public.enforce_analysis_quota()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  piano   text;
  limite  integer;
  oggi    integer;
begin
  select s.tier into piano
    from public.subscriptions s
   where s.user_id = new.user_id;

  limite := public.analysis_limit_for_tier(piano);

  -- `>= date_trunc('day', now())` e non `created_at::date = current_date`:
  -- la seconda forma non e' sargable e ignorerebbe l'indice, trasformando un
  -- controllo su poche righe in una scansione. Il confronto e' in UTC, come
  -- `now()` sul database.
  select count(*) into oggi
    from public.analysis_events e
   where e.user_id = new.user_id
     and e.created_at >= date_trunc('day', now());

  if oggi >= limite then
    -- SQLSTATE di dominio dedicato, distinto da PX001 del tetto creator: il
    -- client deve poter dire "hai esaurito le analisi di oggi" e non "hai
    -- troppi creator". Senza un codice proprio finirebbe nel ramo generico e
    -- l'utente leggerebbe "servizio dati non disponibile".
    raise exception using
      errcode = 'PX002',
      message = format(
        'Quota di %s analisi giornaliere raggiunta per il piano %s.',
        limite, coalesce(piano, 'free')
      );
  end if;

  return new;
end;
$$;

comment on function public.enforce_analysis_quota() is
  'Limita le analisi manuali giornaliere per utente in base a '
  'subscriptions.tier. Vale per chiunque scriva su analysis_events: e'' il '
  'database l''arbitro, non il codice applicativo — quindi due richieste '
  'concorrenti al tetto non possono superarlo entrambe.';

revoke execute on function public.enforce_analysis_quota() from public;

drop trigger if exists analysis_events_enforce_quota on public.analysis_events;

create trigger analysis_events_enforce_quota
  before insert on public.analysis_events
  for each row
  execute function public.enforce_analysis_quota();


-- -----------------------------------------------------------------------------
-- 4. Privilegi: nessuno, come per ogni tabella di coordinamento
-- -----------------------------------------------------------------------------
-- Stesso trattamento di `analysis_locks`, `job_locks` e `subscriptions`: non e'
-- un dato dell'utente, e non esiste alcuna operazione legittima con cui un
-- client debba leggerlo o scriverlo. `revoke all` copre anche TRUNCATE, che non
-- e' soggetto al RLS — la lezione della `0002`.
--
-- Se un giorno servira' mostrare "ti restano N analisi oggi" nell'interfaccia,
-- la strada non e' concedere SELECT su questa tabella ma esporre il conteggio
-- dal backend, che ha gia' il contesto dell'utente autenticato.

alter table public.analysis_events enable row level security;

revoke all on public.analysis_events from anon, authenticated;

grant all on public.analysis_events to service_role;


-- -----------------------------------------------------------------------------
-- 5. LA TABELLA CRESCE SENZA LIMITE — va potata
-- -----------------------------------------------------------------------------
-- Una riga per analisi, per sempre. Al tetto `pro` sono 300 righe al giorno per
-- utente: non e' un problema oggi, lo diventa a volume.
--
-- Serve un job periodico che cancelli oltre una finestra di ritenzione (90
-- giorni e' un punto di partenza ragionevole: copre un trimestre di analisi dei
-- costi e la quota guarda solo il giorno corrente). **Non e' scritto in questa
-- migration di proposito**: sarebbe un secondo job pianificato, e oggi non c'e'
-- nemmeno il primo — `CRON_ENABLED` e' `false`.
--
-- Quando si fara', il meccanismo esiste gia' e non va reinventato: la `0005` ha
-- reso `job_locks` generica **sul nome del job** proprio per questo caso. Il
-- codice sara':
--
--     async with job_lock("cleanup:analysis-events", ttl) as ottenuto:
--         if ottenuto:
--             ... delete from analysis_events where created_at < now() - interval '90 days'
--
-- Nessuna migration nuova, nessun modulo nuovo.
