-- =============================================================================
-- 0005 — `job_locks`: un giro per volta, per qualunque job pianificato
-- =============================================================================
--
-- IL PROBLEMA, MISURATO
--
-- `POST /api/v1/cron/check-updates` non ha alcuna coordinazione contro le
-- esecuzioni sovrapposte. Misurato con due richieste concorrenti e 3 creator
-- attivi: **6 chiamate ad Apify invece di 3** — ogni creator scrapato due
-- volte, ogni scraping una run a pagamento — e un picco di **4 analisi in volo
-- con `CRON_MAX_CONCURRENT_ANALYSES = 2`**, perche' ogni esecuzione costruiva
-- il proprio semaforo.
--
-- DA DOVE ARRIVA DAVVERO LA SOVRAPPOSIZIONE
--
-- Non dallo schedule. La configurazione documentata in `app/cron_config.md` usa
-- GitHub Actions con `concurrency: picox-cron`, che mette in coda un secondo
-- giro schedulato invece di accavallarlo. Arriva dal **retry del client**:
--
--     curl --max-time 120 --retry 2 ...
--
-- Se il censimento supera i 120s, curl abortisce e ri-POSTa mentre il server
-- sta ancora elaborando la prima richiesta. Stessa run, stesso gruppo di
-- concorrenza, nessuna guardia. E superare i 120s non e' un caso limite: il
-- censimento era sequenziale — un `await fetch_latest_videos` per creator, con
-- `APIFY_TIMEOUT_SECONDS = 180` ciascuna — quindi al tetto di 30 creator attivi
-- introdotto dalla `0004` bastano ~4s a creator per sfondare la soglia.
--
-- La `0005` chiude il lato server. Il lato client — censimento parallelo,
-- `--max-time` alzato, `--retry` rimosso — e' corretto nello stesso intervento.
--
-- PERCHE UNA TABELLA NUOVA E NON `analysis_locks`
--
-- La chiave di `analysis_locks` e' `(user_id, video_url, analysis_mode)`. Un job
-- pianificato non ha nessuna delle tre: riusarla richiederebbe valori
-- sentinella, e una chiave che mente sul proprio significato e' un debito che si
-- paga alla prima persona che la legge. Il **meccanismo** invece e' identico, ed
-- e' quello gia' verificato dalla `0003`: due passi atomici, scadenza esplicita,
-- arbitrato dal database e non dal processo, valido anche fra istanze diverse.
--
-- LA CHIAVE E' UN NOME DI JOB, NON "IL CRON"
--
-- Deliberato: quando arrivera' un secondo job pianificato — un worker di
-- riconciliazione, una pulizia periodica, qualunque cosa — gli bastera' passare
-- il proprio nome. Nessuna migration nuova, nessun modulo nuovo, nessun
-- registro da tenere aggiornato. E' il minimo che rende il meccanismo
-- riutilizzabile senza costruire un framework di scheduling che non serve a
-- nessuno.
--
-- IL TTL QUI NON E' UN VINCOLO DI CORRETTEZZA
--
-- Nella `0003` lo era: se il lock fosse scaduto durante un'analisi in corso, una
-- seconda richiesta l'avrebbe sottratto e la doppia spesa sarebbe tornata.
-- Qui no. Un giro completo puo' durare, nel caso pessimo, decine di ore — 30
-- creator x 10 risultati, analisi da 1200s ciascuna a due per volta — e un TTL
-- che coprisse quel caso sarebbe piu' lungo dello schedule stesso (6 ore),
-- bloccando il cron per sempre invece di proteggerlo.
--
-- `CRON_LOCK_TTL_SECONDS = 1800` e' quindi **il limite di quanto accettiamo di
-- restare fermi per un processo morto**, non la durata massima di un giro. Sta
-- sopra un giro realistico completo e sotto lo schedule di un fattore 12. Se
-- scade durante un giro legittimamente lungo il degrado e' grazioso: si torna al
-- comportamento precedente a questa migration, non a qualcosa di peggio, perche'
-- le analisi a valle restano protette da `analysis_locks`.

create table if not exists public.job_locks (
  -- Identificatore del job. `text` e non un enum: aggiungere un job non deve
  -- richiedere una migration.
  job_name    text        primary key,
  locked_at   timestamptz not null default now(),
  expires_at  timestamptz not null,
  -- Chi lo detiene, per il debug: senza, davanti a un lock scaduto e sottratto
  -- non c'e' modo di sapere quale istanza fosse morta.
  holder      text
);

comment on table public.job_locks is
  'Lock a scadenza per job pianificati, uno per `job_name`. Garantisce che un '
  'giro per volta sia in esecuzione, anche fra istanze diverse. Nessun client '
  'la vede: la usa solo il backend col client service-role.';

comment on column public.job_locks.expires_at is
  'Oltre questo istante il lock e'' considerato abbandonato e puo'' essere '
  'sottratto, senza che il detentore precedente abbia eseguito alcun rilascio. '
  'E'' la difesa contro il processo che muore a meta'' giro — deploy, OOM, '
  'macchina che sparisce — dove nessun `finally` viene mai eseguito.';

comment on column public.job_locks.holder is
  'Identificativo di chi ha preso il lock (host/pid). Solo diagnostico: non '
  'partecipa ad alcuna decisione, perche'' fidarsi di un valore scritto dal '
  'detentore precedente reintrodurrebbe il problema che la scadenza risolve.';

create index if not exists job_locks_expires_at_idx
  on public.job_locks (expires_at);


-- =============================================================================
-- Privilegi
-- =============================================================================
-- Identici a `analysis_locks`: e' coordinamento interno al backend, non un dato
-- dell'utente. RLS resta abilitato senza policy — nega tutto — cosi' se un
-- domani un GRANT venisse concesso per errore la tabella non diventerebbe
-- comunque leggibile. Difesa in profondita', non ridondanza.

alter table public.job_locks enable row level security;

revoke all on public.job_locks from anon, authenticated;

grant all on public.job_locks to service_role;
