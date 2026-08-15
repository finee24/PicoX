-- =============================================================================
-- 0010 — Validazione dei creator: cache condivisa e quota per utente
-- =============================================================================
--
-- COSA INTRODUCE
--
-- `POST /api/v1/creators/validate` verifica che un account esista e sia
-- pubblico prima che l'utente lo aggiunga alla watchlist. Per farlo chiama
-- **API esterne a consumo**: un actor Apify per Instagram e TikTok, la YouTube
-- Data API v3 per YouTube. Due tabelle, con due scopi diversi:
--
--   * `creator_validations` — cache del risultato, condivisa fra tutti gli
--     utenti, con TTL applicativo di 24h;
--   * `validation_events`   — una riga per validazione **pagata**, che e' cio'
--     su cui il trigger impone il tetto giornaliero per utente.
--
-- PERCHE SERVONO ENTRAMBE (la cache da sola non e' un rate limit)
--
-- E' la stessa lezione della `0008`, vista da un'altra angolazione. La cache
-- azzera il costo delle chiamate **ripetute sullo stesso identificatore**, ma
-- non tocca il vettore vero: un utente che valida 5.000 handle *diversi* in un
-- minuto produce 5.000 cache miss, cioe' 5.000 chiamate esterne reali. Ogni
-- miss e' denaro. Il tetto e' l'unica cosa che chiude quel caso, ed e' il
-- motivo per cui questo endpoint non e' pronto al merge con la sola cache.
--
-- LA CACHE E' GLOBALE, E DEVE ESSERLO
--
-- La chiave e' `(platform, normalized_identifier)`, senza `user_id`. Una cache
-- per utente moltiplicherebbe la spesa esterna per il numero di utenti
-- interessati allo stesso creator — cioe' proprio la spesa che questa tabella
-- esiste per evitare, visto che i creator popolari sono popolari per
-- definizione.
--
-- E' ammissibile perche' il contenuto **non e' un dato di alcun utente**: nome,
-- avatar, follower e stato pubblico/privato di un profilo sono gia' visibili a
-- chiunque apra quel profilo. Non c'e' un tenant da separare. Cio' che invece
-- resta strettamente per utente — *chi* ha validato *cosa* — sta in
-- `validation_events`, che infatti ha `user_id` ed e' scopata come le altre.
--
-- Lato applicativo la conseguenza e' visibile: `creator_validations` e' la
-- terza tabella ammessa in `_UNSCOPED_ALLOWED` (`supabase_service.py`), accanto
-- a `creators` e `job_locks`, e per la stessa ragione delle altre due — non ha
-- una colonna su cui scopare, non perche' sia comodo non filtrare.

-- -----------------------------------------------------------------------------
-- 1. La cache
-- -----------------------------------------------------------------------------

create table if not exists public.creator_validations (
  platform              text        not null check (platform in ('instagram', 'tiktok', 'youtube_shorts')),
  -- Handle normalizzato: minuscolo, senza '@', senza URL attorno. La
  -- normalizzazione la fa `creator_validation.parse_creator_input` in Python;
  -- se qui finisse la forma grezza, `@Tizio` e `instagram.com/tizio/` sarebbero
  -- due righe di cache per lo stesso profilo e la seconda ripagherebbe Apify.
  normalized_identifier text        not null,
  -- La risposta gia' composta, nella forma di `CreatorValidationResponse`.
  -- Denormalizzata di proposito: la cache deve poter restituire esattamente
  -- cio' che l'endpoint restituirebbe, senza ricomporlo da colonne separate che
  -- divergerebbero dallo schema al primo campo aggiunto.
  response              jsonb       not null,
  checked_at            timestamptz not null default now(),

  primary key (platform, normalized_identifier)
);

comment on table public.creator_validations is
  'Cache condivisa delle validazioni di profilo (esiste / e'' pubblico / '
  'anteprima). Nessun dato di utente: solo informazioni gia'' pubbliche sul '
  'profilo. Il TTL e'' applicativo (Settings.creator_validation_ttl_hours, 24h '
  'di default), non un vincolo di questa tabella.';

comment on column public.creator_validations.checked_at is
  'Istante dell''ultima verifica reale contro il provider. E'' la colonna su cui '
  'il backend decide se la riga e'' ancora fresca: oltre il TTL si rivalida e si '
  'sovrascrive, non si cancella.';

-- L'indice serve alla potatura per eta' (vedi sezione 5), non alla lettura:
-- quella passa sempre dalla primary key.
create index if not exists creator_validations_checked_at_idx
  on public.creator_validations (checked_at);


-- -----------------------------------------------------------------------------
-- 2. Il tetto per piano, accanto ai suoi due gemelli
-- -----------------------------------------------------------------------------
-- Stessa forma di `creator_limit_for_tier` (0007) e `analysis_limit_for_tier`
-- (0008), e per la stessa ragione: i numeri stanno in un posto solo. Tre `CASE`
-- copiati divergono alla prima modifica.
--
-- I NUMERI SONO SEGNAPOSTO, E LO SONO DICHIARATAMENTE
--
-- Costo per validazione: ~$0,003 di Apify per un profilo Instagram o TikTok.
-- Per YouTube il costo monetario e' zero, ma la risorsa scarsa esiste lo stesso
-- ed e' **globale al progetto**: la YouTube Data API concede 10.000 unita' al
-- giorno e una `channels.list` ne consuma 1. Il tetto per utente e' quindi
-- anche cio' che impedisce a un singolo account di bruciare la quota YouTube di
-- tutti gli altri.
--
--   free   50 validazioni/giorno  ~$0,15/giorno, ≤ 50 unita' YouTube
--   pro   500 validazioni/giorno  ~$1,50/giorno, ≤ 500 unita' YouTube
--
-- Piu' alti dei tetti sulle analisi perche' una validazione costa un ordine di
-- grandezza meno e sta su un gesto di interfaccia — si valida scrivendo un
-- handle in un campo, non lanciando un'analisi. Restano **derivati da listini
-- pubblici e non misurati**, come gli altri due: e' `validation_events` a
-- rendere possibile misurarli.

create or replace function public.validation_limit_for_tier(tier text)
returns integer
language sql
immutable
set search_path = ''
as $$
  select case coalesce(tier, 'free')
           when 'pro' then 500
           else 50
         end;
$$;

comment on function public.validation_limit_for_tier(text) is
  'Validazioni di creator al giorno ammesse per un dato piano. Numeri '
  'segnaposto, derivati dal costo per validazione (~$0,003 Apify, 1 unita'' '
  'della quota YouTube) e non misurati: vanno fissati contro il prezzo del '
  'piano quando esistera'', come per gli altri due limiti.';

revoke execute on function public.validation_limit_for_tier(text) from public;


-- -----------------------------------------------------------------------------
-- 3. Una riga per validazione pagata
-- -----------------------------------------------------------------------------
-- Append-only e non un contatore aggregato, per lo stesso motivo della `0008`:
-- `conteggio = conteggio + 1` non e' esprimibile con un upsert PostgREST e
-- richiederebbe una RPC, cioe' una terza via d'accesso al database accanto alle
-- due che `supabase_service` dichiara essere le uniche.

create table if not exists public.validation_events (
  id                    uuid        primary key default gen_random_uuid(),
  user_id               uuid        not null references auth.users (id) on delete cascade,
  -- Denormalizzati per la stessa ragione di `analysis_events`: rendono la
  -- tabella utile alla misura dei costi senza dover ricostruire nulla. La
  -- piattaforma dice *quale* provider e' stato pagato, l'identificatore
  -- distingue un abuso distribuito da un uso legittimo concentrato.
  platform              text        not null check (platform in ('instagram', 'tiktok', 'youtube_shorts')),
  normalized_identifier text        not null,
  created_at            timestamptz not null default now()
);

comment on table public.validation_events is
  'Una riga per validazione che ha davvero chiamato un provider esterno. I '
  'cache hit non compaiono: non costano nulla, quindi non consumano quota — '
  'stessa regola dei cache hit su insights in analysis_events.';

comment on column public.validation_events.created_at is
  'Istante della chiamata esterna. E'' la colonna su cui si conta la quota del '
  'giorno, in UTC: il "giorno" e'' quello del database, non quello del fuso '
  'dell''utente.';

create index if not exists validation_events_user_created_idx
  on public.validation_events (user_id, created_at desc);


-- -----------------------------------------------------------------------------
-- 4. Il trigger che impone la quota
-- -----------------------------------------------------------------------------
-- BEFORE INSERT e non un CHECK: il vincolo dipende da altre righe della stessa
-- tabella e dal piano dell'utente, cose che un CHECK non puo' vedere.
--
-- L'arbitro e' il database e non il codice applicativo: due richieste
-- concorrenti al tetto non possono superarlo entrambe, cosa che un controllo
-- leggi-poi-scrivi in Python non garantirebbe.

create or replace function public.enforce_validation_quota()
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

  limite := public.validation_limit_for_tier(piano);

  -- `>= date_trunc('day', now())` e non `created_at::date = current_date`: la
  -- seconda forma non e' sargable e ignorerebbe l'indice, trasformando un
  -- controllo su poche righe in una scansione.
  select count(*) into oggi
    from public.validation_events e
   where e.user_id = new.user_id
     and e.created_at >= date_trunc('day', now());

  if oggi >= limite then
    -- Terzo SQLSTATE di dominio, distinto da PX001 (tetto creator) e PX002
    -- (quota analisi). Sono tre limiti diversi e richiedono all'utente tre
    -- azioni diverse: un codice unico produrrebbe un messaggio generico
    -- proprio dove servono istruzioni precise.
    raise exception using
      errcode = 'PX003',
      message = format(
        'Quota di %s validazioni giornaliere raggiunta per il piano %s.',
        limite, coalesce(piano, 'free')
      );
  end if;

  return new;
end;
$$;

comment on function public.enforce_validation_quota() is
  'Limita le validazioni di creator giornaliere per utente in base a '
  'subscriptions.tier. Vale per chiunque scriva su validation_events: e'' il '
  'database l''arbitro, non il codice applicativo.';

revoke execute on function public.enforce_validation_quota() from public;

drop trigger if exists validation_events_enforce_quota on public.validation_events;

create trigger validation_events_enforce_quota
  before insert on public.validation_events
  for each row
  execute function public.enforce_validation_quota();


-- -----------------------------------------------------------------------------
-- 5. Privilegi: nessuno per i client, come per ogni tabella di servizio
-- -----------------------------------------------------------------------------
-- Stesso trattamento di `analysis_locks`, `job_locks`, `subscriptions` e
-- `analysis_events`. `revoke all` copre anche TRUNCATE, che non e' soggetto al
-- RLS — la lezione della `0002`.
--
-- Per `creator_validations` la ragione **non** e' la riservatezza del
-- contenuto, che e' pubblico: e' che un client con accesso diretto potrebbe
-- scriverci dentro. Una riga di cache falsificata farebbe risultare pubblico un
-- profilo privato, o esistente un profilo inventato, per **tutti** gli utenti e
-- per 24h. La cache la scrive solo chi ha appena parlato col provider.

alter table public.creator_validations enable row level security;

revoke all on public.creator_validations from anon, authenticated;

grant all on public.creator_validations to service_role;

alter table public.validation_events enable row level security;

revoke all on public.validation_events from anon, authenticated;

grant all on public.validation_events to service_role;


-- -----------------------------------------------------------------------------
-- 6. ANCHE QUESTE TABELLE VANNO POTATE
-- -----------------------------------------------------------------------------
-- `validation_events` cresce come `analysis_events` e va potata con la stessa
-- finestra di ritenzione, nello stesso job che oggi non esiste (vedi la nota in
-- coda alla `0008`: il meccanismo e' gia' pronto, `job_locks` e' generica sul
-- nome del job).
--
-- `creator_validations` invece **non cresce senza limite in modo pericoloso**:
-- e' limitata dal numero di profili distinti mai validati, e ogni rivalidazione
-- sovrascrive la riga esistente invece di aggiungerne una. Vale comunque la
-- pena cancellare le righe non piu' rilette da mesi, ed e' a questo che serve
-- `creator_validations_checked_at_idx`.
