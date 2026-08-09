-- =============================================================================
-- 0003 — `analysis_locks`: deduplica delle analisi in corso
-- =============================================================================
--
-- IL PROBLEMA
--
-- `UNIQUE (user_id, video_url)` su `insights` impedisce la riga duplicata, ma
-- non il **lavoro** duplicato. Fra la lettura della cache e la scrittura del
-- risultato passa l'intera pipeline — Apify, download, inferenza Gemini — e in
-- quella finestra nulla impedisce a una seconda richiesta di ripartire da zero.
-- Misurato: 3 POST concorrenti sullo stesso video producono 3 chiamate Gemini,
-- 3 run Apify e 3 download, per finire in 1 sola riga. Il vincolo deduplica il
-- risultato dopo che si è pagato tre volte per ottenerlo.
--
-- PERCHÉ UNA TABELLA E NON UN ADVISORY LOCK
--
-- `pg_advisory_lock` sarebbe il meccanismo naturale — si rilascia da solo alla
-- chiusura della sessione, quindi niente lock orfani. Ma questo backend non ha
-- una connessione Postgres da tenere aperta: parla con il database solo
-- attraverso PostgREST via HTTP (nessun `asyncpg`, nessun `psycopg` fra le
-- dipendenze). Un lock di sessione richiederebbe di trattenere quella sessione
-- per tutta l'analisi, cosa che PostgREST non consente e che il pooler di
-- Supabase renderebbe comunque inaffidabile; un `pg_advisory_xact_lock` dentro
-- una RPC vivrebbe solo per la transazione di quella chiamata — millisecondi,
-- non i minuti che dura un'analisi. Preso su una connessione poolata, un lock
-- di sessione resterebbe orfano: esattamente ciò che si vuole evitare.
--
-- Restano quindi le righe di una tabella, con una scadenza esplicita.
--
-- COSA SUCCEDE SE CHI DETIENE IL LOCK MUORE
--
-- È il caso che conta, e non può dipendere da un `finally`: un processo ucciso
-- da OOM, un container riavviato durante un deploy o una macchina che sparisce
-- non eseguono nulla in uscita. Per questo il lock **non è un semplice INSERT
-- che qualcuno deve ricordarsi di cancellare**: ogni riga porta `expires_at`, e
-- l'acquisizione è definita in due passi, entrambi atomici in Postgres:
--
--   1. INSERT: riesce solo se per quella chiave non esiste alcuna riga;
--   2. se l'INSERT collide, UPDATE ... WHERE expires_at <= now(): riesce solo
--      se il lock esistente è **scaduto**, e in tal caso lo sottrae al
--      detentore precedente rinnovandone la scadenza.
--
-- Un lock abbandonato viene quindi riassorbito dalla prima richiesta che arriva
-- dopo la sua scadenza, senza processi di pulizia, senza cron e senza che
-- nessuno debba aver eseguito codice di rilascio. Il rilascio esplicito in
-- `finally` resta, ma è solo un'ottimizzazione del caso normale: libera la
-- chiave subito invece di far aspettare la scadenza.
--
-- LA DURATA DELLA SCADENZA È UN VINCOLO, NON UNA PREFERENZA
--
-- `expires_at` deve superare la durata massima di un'analisi legittima. Se
-- scadesse prima, una seconda richiesta ruberebbe il lock a un'analisi ancora
-- in corso e la doppia spesa tornerebbe — con in più due scrittori sulla stessa
-- riga. Sommando i timeout configurati: Apify 180s + download 120s + attesa
-- del file Gemini 120s + inferenza 300s × 2 tentativi = 1020s. Il default
-- applicativo è 1200s, che lascia margine senza essere arbitrario.
-- Vedi `Settings.analysis_lock_ttl_seconds`.

create table if not exists public.analysis_locks (
  user_id       uuid        not null references auth.users (id) on delete cascade,
  video_url     text        not null,
  analysis_mode text        not null check (analysis_mode in ('INFO', 'STYLE', 'BOTH')),
  locked_at     timestamptz not null default now(),
  expires_at    timestamptz not null,

  -- La granularità della chiave è esattamente quella della riga contesa: due
  -- utenti sullo stesso video, o lo stesso utente su video o modalità diverse,
  -- non si bloccano a vicenda. Coincide con ciò che `perform_analysis`
  -- userebbe per decidere se un lavoro è già stato fatto.
  primary key (user_id, video_url, analysis_mode)
);

comment on table public.analysis_locks is
  'Lock a scadenza sulle analisi in corso. Nessuno lo legge o scrive se non il '
  'backend col client service-role: evita di pagare due volte Apify e Gemini '
  'per lo stesso (utente, video, modalità).';

comment on column public.analysis_locks.expires_at is
  'Oltre questo istante il lock è considerato abbandonato e può essere '
  'sottratto da chiunque, senza che il detentore precedente abbia eseguito '
  'alcun rilascio. È la difesa contro i lock orfani lasciati da un crash.';

-- L'acquisizione filtra sempre su `expires_at`; l'indice serve anche a una
-- eventuale pulizia massiva delle righe scadute, che però non è necessaria:
-- ogni riga scaduta viene riusata o sovrascritta dalla richiesta successiva
-- sulla stessa chiave.
create index if not exists analysis_locks_expires_at_idx
  on public.analysis_locks (expires_at);


-- =============================================================================
-- Privilegi
-- =============================================================================
-- Nessun client, autenticato o no, ha motivo di vedere o toccare questa
-- tabella: è un dettaglio di coordinamento interno al backend, non un dato
-- dell'utente. Il solo `service_role` (BYPASSRLS) la usa.
--
-- RLS resta comunque abilitato: senza policy nega tutto, quindi se un domani
-- un GRANT venisse concesso per errore la tabella non diventerebbe leggibile
-- per quello solo. Difesa in profondità, non ridondanza.

alter table public.analysis_locks enable row level security;

revoke all on public.analysis_locks from anon, authenticated;

grant all on public.analysis_locks to service_role;
