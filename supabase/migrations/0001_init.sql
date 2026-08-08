-- =============================================================================
-- Picox — Migration 0001 · Schema iniziale
-- Target: PostgreSQL 15+ su Supabase (schema `auth` gestito da GoTrue)
-- =============================================================================
--
-- Contenuto:
--   1. Funzioni di utilità   (set_updated_at, handle_new_user)
--   2. Tabelle               (profiles, creators, insights)
--   3. Trigger
--   4. Indici
--   5. Grants
--   6. Row Level Security + Policy
--
-- -----------------------------------------------------------------------------
-- ATTENZIONE — LA SERVICE ROLE KEY BYPASSA IL ROW LEVEL SECURITY (BY DESIGN)
-- -----------------------------------------------------------------------------
-- Le policy definite in questo file proteggono SOLO le connessioni che usano la
-- ANON KEY con un JWT utente valido (browser / mobile), perché il ruolo
-- `authenticated` è soggetto a RLS.
--
-- Il ruolo `service_role`, usato dal backend con SUPABASE_SERVICE_ROLE_KEY, ha
-- l'attributo BYPASSRLS: nessuna policy viene valutata. Di conseguenza:
--
--   -> OGNI query eseguita dal backend con il client service-role DEVE filtrare
--      manualmente su `user_id` (su `id` per `profiles`), ricavando l'ID dal JWT
--      dell'utente già verificato — mai da un parametro della request.
--
--         OK   .from('insights').select('*').eq('user_id', userIdFromJwt)
--         NO   .from('insights').select('*')
--         NO   .from('insights').select('*').eq('user_id', req.body.userId)
--
--   -> Lo stesso vale per UPDATE e DELETE: sempre `.eq('user_id', userIdFromJwt)`
--      IN AGGIUNTA al filtro sulla primary key, altrimenti un UUID indovinato o
--      trapelato consente scritture cross-tenant.
--
--   -> La SERVICE_ROLE_KEY non deve mai raggiungere il browser né essere esposta
--      tramite variabili d'ambiente pubbliche (NEXT_PUBLIC_*, VITE_*).
--
-- Responsabilità di implementazione: Agente 2 (Backend).
-- =============================================================================


-- =============================================================================
-- 1. FUNZIONI DI UTILITÀ
-- =============================================================================

-- Aggiorna `updated_at` a ogni UPDATE. Funzione generica e riutilizzabile:
-- va agganciata a qualsiasi tabella che espone una colonna `updated_at`.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
-- search_path vuoto: previene search_path hijacking; tutti gli oggetti
-- referenziati nel corpo devono essere qualificati con lo schema.
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

comment on function public.set_updated_at() is
  'Trigger function riutilizzabile (BEFORE UPDATE): imposta new.updated_at = now().';


-- Crea automaticamente la riga in `public.profiles` al signup.
-- SECURITY DEFINER è obbligatorio: il trigger viene eseguito nel contesto del
-- ruolo `supabase_auth_admin`, che non ha privilegi sullo schema `public`.
-- La funzione gira quindi con i privilegi del proprietario (`postgres`).
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing; -- idempotente: il signup non deve mai fallire
  return new;
end;
$$;

comment on function public.handle_new_user() is
  'Trigger function (AFTER INSERT ON auth.users): popola public.profiles al signup. SECURITY DEFINER.';


-- =============================================================================
-- 2. TABELLE
-- =============================================================================

-- -----------------------------------------------------------------------------
-- profiles — estensione applicativa di auth.users (1:1)
-- -----------------------------------------------------------------------------
-- La PK è anche FK verso auth.users: un profilo non può esistere senza account,
-- e la cancellazione dell'account elimina il profilo (ON DELETE CASCADE).
create table public.profiles (
  id                 uuid        primary key references auth.users (id) on delete cascade,
  email              text,
  subscription_tier  text        not null default 'free'
                                 check (subscription_tier in ('free', 'pro')),
  created_at         timestamptz not null default now()
);

comment on table  public.profiles is
  'Profilo applicativo dell''utente, in relazione 1:1 con auth.users. La riga è creata dal trigger on_auth_user_created.';
comment on column public.profiles.id is
  'PK e FK verso auth.users(id). Chiave di scoping RLS per questa tabella (auth.uid() = id).';
comment on column public.profiles.subscription_tier is
  'Piano di abbonamento: free | pro. Guida i limiti di quota applicati dal backend.';


-- -----------------------------------------------------------------------------
-- creators — account monitorati dall'utente
-- -----------------------------------------------------------------------------
create table public.creators (
  id             uuid        primary key default gen_random_uuid(),
  user_id        uuid        not null references auth.users (id) on delete cascade,
  username       text        not null,
  platform       text        not null
                             check (platform in ('instagram', 'tiktok', 'youtube_shorts')),
  analysis_mode  text        not null default 'BOTH'
                             check (analysis_mode in ('INFO', 'STYLE', 'BOTH')),
  is_active      boolean     not null default true,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  -- Lo stesso username sulla stessa piattaforma può essere monitorato da utenti
  -- diversi, ma una sola volta per utente: rende l'inserimento idempotente
  -- (ON CONFLICT DO NOTHING / DO UPDATE lato backend).
  constraint creators_user_id_username_platform_key unique (user_id, username, platform)
);

comment on table  public.creators is
  'Creator monitorati da un utente. Sorgente dello scraping periodico (cron).';
comment on column public.creators.user_id is
  'Proprietario della riga. Chiave di scoping RLS e filtro obbligatorio lato backend service-role.';
comment on column public.creators.analysis_mode is
  'Modalità di analisi di default applicata ai video di questo creator: INFO | STYLE | BOTH.';
comment on column public.creators.is_active is
  'Soft-switch del monitoraggio: false esclude il creator dal cron senza perdere gli insight storici.';


-- -----------------------------------------------------------------------------
-- insights — risultato dell'analisi AI multimodale di un singolo video
-- -----------------------------------------------------------------------------
create table public.insights (
  id                       uuid        primary key default gen_random_uuid(),

  -- Nullable + ON DELETE SET NULL: un insight sopravvive alla rimozione del
  -- creator monitorato (l'analisi è già stata pagata in token AI) ed è coerente
  -- con l'ingestione da link singolo, dove nessun creator è coinvolto.
  creator_id               uuid        references public.creators (id) on delete set null,

  user_id                  uuid        not null references auth.users (id) on delete cascade,
  video_url                text        not null,
  thumbnail_url            text,
  analysis_mode            text        not null
                                       check (analysis_mode in ('INFO', 'STYLE', 'BOTH')),
  summary_data             jsonb,
  style_data               jsonb,
  inverse_script_template  jsonb,
  keywords                 text[]      default '{}'::text[],
  created_at               timestamptz not null default now(),

  -- Cache anti-duplicati: lo stesso video non viene mai rianalizzato due volte
  -- per lo stesso utente. Il backend fa lookup su (user_id, video_url) prima di
  -- invocare Gemini/Apify e, in caso di hit, restituisce la riga esistente.
  constraint insights_user_id_video_url_key unique (user_id, video_url)
);

comment on table  public.insights is
  'Output dell''analisi AI di un video breve. Una riga per (utente, video): vedi insights_user_id_video_url_key.';
comment on column public.insights.creator_id is
  'Creator di provenienza. NULL per ingestione da link singolo o dopo la rimozione del creator (ON DELETE SET NULL).';
comment on column public.insights.user_id is
  'Proprietario della riga. Chiave di scoping RLS e filtro obbligatorio lato backend service-role.';
comment on column public.insights.summary_data is
  'Payload INFO: sintesi dei contenuti prodotta dal modello multimodale.';
comment on column public.insights.style_data is
  'Payload STYLE: hook, ritmo, editing, tono, CTA.';
comment on column public.insights.inverse_script_template is
  'Script inverso: struttura riusabile derivata dal video, pronta per essere riadattata.';
comment on column public.insights.keywords is
  'Keyword estratte. Indicizzata GIN per ricerca con operatori array (@>, &&).';


-- =============================================================================
-- 3. TRIGGER
-- =============================================================================

-- Signup -> profilo. Il trigger vive sullo schema `auth`, gestito da GoTrue.
-- `or replace` (PG14+) invece di DROP + CREATE: mantiene la migration
-- ri-eseguibile su un DB già inizializzato senza richiedere l'ownership di
-- `auth.users` (che appartiene a supabase_auth_admin, non a postgres).
create or replace trigger on_auth_user_created
  after insert on auth.users
  for each row
  execute function public.handle_new_user();

-- Manutenzione di updated_at.
create trigger set_creators_updated_at
  before update on public.creators
  for each row
  execute function public.set_updated_at();


-- =============================================================================
-- 4. INDICI
-- =============================================================================

-- Ricerca per keyword: array_ops supporta @> (contiene), && (interseca), <@.
create index insights_keywords_idx on public.insights using gin (keywords);

-- Scoping per tenant: percorso di accesso di ogni query lato applicazione ed
-- efficienza delle policy RLS (che filtrano sempre su user_id).
create index creators_user_id_idx  on public.creators (user_id);
create index insights_user_id_idx  on public.insights (user_id);

-- Feed di un singolo creator + supporto al SET NULL in cascata sulla DELETE
-- (senza indice, ogni cancellazione di creator richiederebbe un seq scan).
create index insights_creator_id_idx on public.insights (creator_id);

-- Lookup per URL a prescindere dall'utente. Il constraint UNIQUE già copre il
-- prefisso (user_id, video_url); questo indice serve al caso complementare:
-- verificare se un video è già stato analizzato nel sistema (cross-tenant),
-- per riutilizzare l'output AI senza duplicare il costo di inferenza.
create index insights_video_url_idx on public.insights (video_url);


-- =============================================================================
-- 5. GRANTS
-- =============================================================================
-- Espliciti per non dipendere dalle default privileges del progetto.
-- `anon` non riceve alcun privilegio: nessun dato di Picox è pubblico.

revoke all on public.profiles, public.creators, public.insights from anon;

grant select, insert, update, delete
  on public.profiles, public.creators, public.insights
  to authenticated; -- comunque filtrato dalle policy RLS della sezione 6

grant all
  on public.profiles, public.creators, public.insights
  to service_role;  -- BYPASSRLS: lo scoping è responsabilità del backend


-- =============================================================================
-- 6. ROW LEVEL SECURITY
-- =============================================================================
-- Una policy per comando (SELECT/INSERT/UPDATE/DELETE) invece di FOR ALL:
-- rende esplicito il permesso concesso e semplifica revoche mirate.
--
-- `(select auth.uid())` invece di `auth.uid()`: PostgreSQL valuta la subquery
-- una sola volta come InitPlan anziché per ogni riga — differenza sostanziale
-- sulle scansioni ampie.

alter table public.profiles enable row level security;
alter table public.creators enable row level security;
alter table public.insights enable row level security;


-- -----------------------------------------------------------------------------
-- profiles — scoping su `id` (la PK coincide con auth.users.id)
-- -----------------------------------------------------------------------------
create policy "profiles_select_own" on public.profiles
  for select to authenticated
  using ((select auth.uid()) = id);

-- INSERT normalmente superfluo (ci pensa on_auth_user_created), previsto come
-- fallback per il self-healing di un profilo mancante.
create policy "profiles_insert_own" on public.profiles
  for insert to authenticated
  with check ((select auth.uid()) = id);

create policy "profiles_update_own" on public.profiles
  for update to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

create policy "profiles_delete_own" on public.profiles
  for delete to authenticated
  using ((select auth.uid()) = id);


-- -----------------------------------------------------------------------------
-- creators — scoping su `user_id`
-- -----------------------------------------------------------------------------
create policy "creators_select_own" on public.creators
  for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "creators_insert_own" on public.creators
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

-- WITH CHECK sulla UPDATE impedisce di riassegnare la riga a un altro utente.
create policy "creators_update_own" on public.creators
  for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "creators_delete_own" on public.creators
  for delete to authenticated
  using ((select auth.uid()) = user_id);


-- -----------------------------------------------------------------------------
-- insights — scoping su `user_id`
-- -----------------------------------------------------------------------------
create policy "insights_select_own" on public.insights
  for select to authenticated
  using ((select auth.uid()) = user_id);

-- Oltre al proprietario della riga si valida la coerenza del riferimento:
-- `creator_id` deve essere NULL (link singolo) oppure puntare a un creator dello
-- stesso utente. Senza questo controllo un client autenticato potrebbe agganciare
-- i propri insight al creator di un altro tenant.
create policy "insights_insert_own" on public.insights
  for insert to authenticated
  with check (
    (select auth.uid()) = user_id
    and (
      creator_id is null
      or exists (
        select 1
        from public.creators c
        where c.id = creator_id
          and c.user_id = (select auth.uid())
      )
    )
  );

create policy "insights_update_own" on public.insights
  for update to authenticated
  using ((select auth.uid()) = user_id)
  with check (
    (select auth.uid()) = user_id
    and (
      creator_id is null
      or exists (
        select 1
        from public.creators c
        where c.id = creator_id
          and c.user_id = (select auth.uid())
      )
    )
  );

create policy "insights_delete_own" on public.insights
  for delete to authenticated
  using ((select auth.uid()) = user_id);
