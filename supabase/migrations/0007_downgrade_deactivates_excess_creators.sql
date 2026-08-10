-- =============================================================================
-- 0007 — Il downgrade disattiva i creator in eccedenza
-- =============================================================================
--
-- IL PROBLEMA (voce A2 di SECURITY_AUDIT.md)
--
-- `enforce_creator_limit` scatta su INSERT e sulla sola transizione
-- `is_active` false->true. Un cambio di piano non tocca `creators`, quindi
-- nessun trigger scattava; e il censimento del cron filtra su `is_active` senza
-- guardare il piano.
--
-- Conseguenza: un `pro` con 150 creator attivi che torna `free` (tetto 30) ne
-- manteneva 150 attivi, e il cron continuava a scraparli a ~$21 al giorno per un
-- utente che non paga piu'. Il tetto valeva solo per chi saliva, mai per chi
-- scendeva.
--
-- LA SCELTA: DISATTIVARE, NON RIFIUTARE NE IGNORARE
--
-- Le tre strade erano disattivare l'eccedenza, rifiutare il downgrade finche'
-- l'utente non scende da se', o accettare il costo. La terza e' quella che si
-- sceglie non decidendo, ed e' quella che paga l'azienda. La seconda tiene in
-- ostaggio un utente che vuole solo smettere di pagare.
--
-- IL CRITERIO: SI MANTENGONO I PIU' VECCHI
--
--     order by created_at desc, id desc   -- i piu' recenti sono i primi a uscire
--
-- Tre ragioni, in ordine di peso:
--
--   1. **E' spiegabile in una frase**: "mantieni i primi 30 che hai aggiunto".
--      Un downgrade e' un momento di perdita, e cio' che succede deve essere
--      anticipabile dall'utente e raccontabile da chi fa supporto senza
--      interrogare il database.
--   2. **Non dipende da `insights`**, l'unico segnale d'uso disponibile, che ha
--      buchi reali: `ON DELETE SET NULL` fa perdere l'attribuzione storica, e un
--      creator aggiunto ieri avrebbe zero insight — verrebbe scartato proprio
--      mentre e' la scelta piu' deliberata dell'utente.
--   3. **Corrisponde al modello mentale del tetto**: avevi 30 posti, ne hai
--      aggiunti altri salendo, tornando giu' riparti dai 30 originali.
--
-- Il contro-argomento — i creator recenti sono quelli a cui l'utente tiene di
-- piu' adesso — e' vero ma debole, perche' **la disattivazione non e'
-- distruttiva**: la riga resta, gli insight storici restano, e l'utente puo'
-- riattivare cio' che vuole disattivando qualcos'altro. Il criterio non decide
-- cosa si perde, decide da dove si riparte.
--
-- `id desc` non e' decorativo: un inserimento in blocco nella stessa transazione
-- produce `created_at` identici, e senza spareggio l'ordine sarebbe indefinito —
-- cioe' due esecuzioni sugli stessi dati potrebbero disattivare creator diversi.

-- -----------------------------------------------------------------------------
-- 1. Il tetto per piano vive in UN posto solo
-- -----------------------------------------------------------------------------
-- Senza questa funzione il `CASE` esisterebbe due volte — in
-- `enforce_creator_limit` e nel trigger qui sotto — ed e' esattamente la
-- divergenza segnalata come B3 nell'audit: il giorno in cui i piani diventano
-- tre, aggiornarne uno solo produce un database che concede cio' che
-- l'applicazione nega, senza che nulla fallisca.

create or replace function public.creator_limit_for_tier(tier text)
returns integer
language sql
immutable
set search_path = ''
as $$
  -- `coalesce` e non `strict`: con STRICT un tier NULL restituirebbe NULL, e un
  -- limite NULL renderebbe vero qualunque confronto sbagliato. Qui l'assenza di
  -- piano ricade sul piu' restrittivo, che e' il default sicuro.
  select case coalesce(tier, 'free')
           when 'pro' then 200
           else 30
         end;
$$;

comment on function public.creator_limit_for_tier(text) is
  'Creator attivi ammessi per un dato piano. Unica sede dei numeri: la usano '
  'sia enforce_creator_limit sia enforce_downgrade_on_subscription, perche'' due '
  'copie dello stesso CASE divergono alla prima modifica.';

revoke execute on function public.creator_limit_for_tier(text) from public;


-- -----------------------------------------------------------------------------
-- 2. `enforce_creator_limit` usa la funzione condivisa
-- -----------------------------------------------------------------------------
-- Identica alla versione della 0006 tranne che per il calcolo del limite.

create or replace function public.enforce_creator_limit()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  piano   text;
  limite  integer;
  attuali integer;
begin
  if tg_op = 'INSERT' and not new.is_active then
    return new;
  end if;
  if tg_op = 'UPDATE' and not (new.is_active and not old.is_active) then
    return new;
  end if;

  select s.tier into piano
    from public.subscriptions s
   where s.user_id = new.user_id;

  limite := public.creator_limit_for_tier(piano);

  select count(*) into attuali
    from public.creators c
   where c.user_id = new.user_id
     and c.is_active
     and c.id <> new.id;

  if attuali >= limite then
    raise exception using
      errcode = 'PX001',
      message = format(
        'Limite di %s creator attivi raggiunto per il piano %s.',
        limite, coalesce(piano, 'free')
      );
  end if;

  return new;
end;
$$;


-- -----------------------------------------------------------------------------
-- 3. Il downgrade
-- -----------------------------------------------------------------------------

create or replace function public.enforce_downgrade_on_subscription()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  limite      integer;
  attuali     integer;
  eccedenza   integer;
  disattivati integer;
  dettaglio   text;
begin
  limite := public.creator_limit_for_tier(new.tier);

  select count(*) into attuali
    from public.creators c
   where c.user_id = new.user_id
     and c.is_active;

  -- Su un upgrade il tetto sale, quindi questa condizione non e' mai vera e la
  -- funzione esce senza toccare nulla. Non serve un ramo dedicato: e' il
  -- confronto stesso a renderla inerte, il che e' meglio di un `if` che
  -- qualcuno potrebbe scrivere al contrario.
  if attuali <= limite then
    return null;
  end if;

  eccedenza := attuali - limite;

  with da_disattivare as (
    select c.id
      from public.creators c
     where c.user_id = new.user_id
       and c.is_active
     -- I piu' recenti escono per primi. Lo spareggio su `id` rende l'ordine
     -- totale anche quando `created_at` coincide.
     order by c.created_at desc, c.id desc
     limit eccedenza
  ),
  aggiornati as (
    -- Le CTE che modificano dati vedono lo snapshot di inizio statement, quindi
    -- selezionare da `creators` mentre la si aggiorna e' ben definito: la
    -- lista da disattivare non risente delle disattivazioni in corso.
    update public.creators c
       set is_active = false
      from da_disattivare d
     where c.id = d.id
    returning c.platform, c.username, c.created_at
  ),
  ordinati as (
    select platform, username,
           row_number() over (order by created_at desc) as riga
      from aggiornati
  )
  select count(*),
         -- Solo i primi 20 nel dettaglio: un downgrade da `pro` puo' toccare
         -- 170 righe, e una riga di log lunga migliaia di caratteri e' una riga
         -- che nessuno legge. Il conteggio invece c'e' sempre, e non e'
         -- filtrato.
         string_agg(format('%s/%s', platform, username), ', ' order by riga)
           filter (where riga <= 20)
    into disattivati, dettaglio
    from ordinati;

  if disattivati > 20 then
    dettaglio := format('%s, e altri %s', dettaglio, disattivati - 20);
  end if;

  -- `raise log` e non `raise notice`: il primo finisce nel log del server, dove
  -- resta consultabile; il secondo va al client, che qui e' il backend o
  -- l'editor SQL, e puo' non essere registrato da nessuna parte. Un downgrade
  -- che disattiva silenziosamente meta' dei creator di qualcuno e' esattamente
  -- il genere di evento che non deve dipendere da chi stava guardando.
  --
  -- Sette segnaposto, sette argomenti: in RAISE il `%%` produce un simbolo di
  -- percentuale letterale e **non** consuma un parametro, quindi un conteggio
  -- sbagliato non e' un dettaglio estetico — e' un errore a runtime, sollevato
  -- proprio mentre si tenta di registrare l'evento.
  raise log
    'Downgrade piano per l''utente %: da % a %, tetto %, attivi % -> disattivati %. Elenco: %',
    new.user_id, old.tier, new.tier, limite, attuali, disattivati,
    coalesce(dettaglio, '-');

  return null;
end;
$$;

comment on function public.enforce_downgrade_on_subscription() is
  'Al calo di piano disattiva i creator attivi eccedenti il nuovo tetto, '
  'mantenendo i piu'' vecchi. Non cancella nulla: la riga e gli insight storici '
  'restano, e l''utente puo'' riattivare cio'' che preferisce disattivando altro.';

revoke execute on function public.enforce_downgrade_on_subscription() from public;

drop trigger if exists subscriptions_enforce_downgrade on public.subscriptions;

create trigger subscriptions_enforce_downgrade
  -- AFTER: il nuovo piano dev'essere gia' quello della riga quando si conta.
  -- `OF tier` piu' la clausola WHEN: senza, il trigger scatterebbe a ogni tocco
  -- della riga — `updated_at` compreso — e rileggerebbe i creator per nulla.
  after update of tier on public.subscriptions
  for each row
  when (new.tier is distinct from old.tier)
  execute function public.enforce_downgrade_on_subscription();


-- -----------------------------------------------------------------------------
-- 4. Cosa questo trigger NON copre, deliberatamente
-- -----------------------------------------------------------------------------
--
--   * **L'INSERT su `subscriptions`.** L'assenza della riga vale gia' `free`,
--     che e' il piano piu' basso: inserirne una puo' solo alzare il tetto o
--     lasciarlo invariato, mai produrre un'eccedenza.
--
--   * **Un abbassamento dei numeri in `creator_limit_for_tier`.** Se una
--     migration futura portasse `free` da 30 a 20, gli utenti gia' sopra
--     resterebbero sopra: nessun UPDATE su `subscriptions` avviene, quindi
--     nessun trigger scatta. Quella migration dovra' fare da se' il rientro
--     delle righe esistenti — la stessa query di questa funzione, applicata a
--     tutti gli utenti. E' scritto qui perche' chi cambiera' quei numeri
--     guardera' questa funzione, non le note di rilascio.
--
--   * **La riattivazione oltre il tetto.** La copre gia' `enforce_creator_limit`
--     con il ramo UPDATE, che infatti **non interferisce** con questo trigger:
--     il suo guard e' `not (new.is_active and not old.is_active)`, e qui si sta
--     disattivando, quindi esce subito. Le due funzioni si dividono il lavoro
--     senza sovrapporsi — una sorveglia chi sale, l'altra chi scende.
