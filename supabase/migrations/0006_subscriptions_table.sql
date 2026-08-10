-- =============================================================================
-- 0006 — Lo stato di pagamento esce da `profiles` ed entra in `subscriptions`
-- =============================================================================
--
-- IL RISCHIO CHE CHIUDE (voce A1 di SECURITY_AUDIT.md)
--
-- La `0001` definisce su `profiles` una policy di UPDATE che copre l'intera riga:
--
--     create policy "profiles_update_own" on public.profiles
--       for update to authenticated
--       using ((select auth.uid()) = id)
--       with check ((select auth.uid()) = id);
--
-- Quella policy consente di aggiornare **qualunque colonna** della propria riga,
-- `subscription_tier` compresa. Finora non faceva danno solo perche' la `0002`
-- aveva revocato il GRANT: **il RLS filtra righe, i GRANT filtrano colonne**, ed
-- era il secondo a reggere, non il primo.
--
-- Il problema e' che la `0002` documenta anche come riaprire, per il caso
-- legittimo in cui si vorra' far modificare l'email:
--
--     grant update (email) on public.profiles to authenticated;
--
-- La forma naturale da digitare, quel giorno, e'
-- `grant update on public.profiles to authenticated` — senza lista di colonne.
-- Da quel momento un `PATCH /rest/v1/profiles?id=eq.<il proprio uuid>` con
-- `{"subscription_tier":"pro"}` funziona, e il trigger della `0004` concede 200
-- creator attivi: ~$28 al giorno di costo per account, gratuiti.
--
-- Non era un bug attivo. Era una mina posata su un percorso che verra' percorso.
--
-- PERCHE UNA TABELLA E NON UNA DIFESA SULLA COLONNA
--
-- Difendere la singola colonna — `revoke update (subscription_tier)` piu' un
-- `WITH CHECK` che la imponga invariata — avrebbe funzionato, ma protegge
-- *quella* colonna. La prossima colonna sensibile che finira' in `profiles`
-- richiedera' di ricordarsi di fare la stessa cosa, e il giorno in cui qualcuno
-- se ne dimentichera' non ci sara' alcun errore a segnalarlo.
--
-- Separare le tabelle sposta la garanzia dal ricordarsi al non poter sbagliare:
-- `subscriptions` non ha **alcun** motivo legittimo di essere scrivibile da un
-- client, quindi la domanda "posso concedere l'update?" non si pone proprio, e
-- non si porra' nemmeno per le colonne che vi si aggiungeranno (`status`,
-- `current_period_end`, l'id del cliente Stripe: vedi Categoria B dell'audit).
--
-- `profiles` resta cio' che e' sempre stata: dati che l'utente possiede e che un
-- giorno potra' modificare. `subscriptions` e' cio' che l'utente non decide.

-- -----------------------------------------------------------------------------
-- 1. La tabella
-- -----------------------------------------------------------------------------

create table if not exists public.subscriptions (
  -- FK verso `auth.users` e non verso `public.profiles`, per coerenza con
  -- `creators`, `insights` e `analysis_locks`, che referenziano tutte
  -- direttamente `auth.users`. Passare da `profiles` accoppierebbe due tabelle
  -- applicative: se il trigger `on_auth_user_created` fallisse, non si potrebbe
  -- creare nemmeno la sottoscrizione — un modo di fallire in piu', in cambio di
  -- nulla.
  user_id     uuid        primary key references auth.users (id) on delete cascade,

  -- Stesso CHECK gia' in uso sulla colonna che sostituisce: i valori ammessi non
  -- cambiano in questa migration, cambia solo dove vivono.
  tier        text        not null default 'free'
                          check (tier in ('free', 'pro')),

  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

comment on table public.subscriptions is
  'Stato di abbonamento dell''utente. Scrivibile SOLO dal service role: non '
  'esiste alcuna operazione legittima con cui un client possa modificare il '
  'proprio piano. Sostituisce profiles.subscription_tier, rimossa dalla 0006.';

comment on column public.subscriptions.tier is
  'Piano: free | pro. L''assenza della riga equivale a ''free'' — vedi il '
  'coalesce in enforce_creator_limit: il default sicuro non dipende dal fatto '
  'che qualcuno abbia ricordato di inserire una riga.';

create trigger set_subscriptions_updated_at
  before update on public.subscriptions
  for each row
  execute function public.set_updated_at();


-- -----------------------------------------------------------------------------
-- 2. Migrazione dei dati
-- -----------------------------------------------------------------------------
-- Idempotente per costruzione: `on conflict do nothing` la rende rieseguibile
-- senza sovrascrivere uno stato piu' recente. Se questa migration venisse
-- riapplicata dopo che il billing ha gia' promosso qualcuno, non lo
-- retrocederebbe.
--
-- Copia **tutte** le righe, comprese quelle a 'free' che coincidono col default.
-- Costa nulla e rende `subscriptions` la fonte di verita' per tutti, invece di
-- una tabella che contiene solo le eccezioni: il coalesce a valle resta una rete
-- di sicurezza, non il meccanismo su cui si regge il caso normale.

insert into public.subscriptions (user_id, tier, created_at)
select p.id, p.subscription_tier, p.created_at
  from public.profiles p
on conflict (user_id) do nothing;


-- -----------------------------------------------------------------------------
-- 3. `enforce_creator_limit` legge dalla nuova tabella
-- -----------------------------------------------------------------------------
-- Va riscritta **prima** del `drop column` qui sotto. I corpi PL/pgSQL non sono
-- validati alla creazione ma all'esecuzione: lasciando la vecchia versione dopo
-- aver rimosso la colonna, la funzione non fallirebbe subito — fallirebbe al
-- primo insert di un creator, cioe' in produzione e non qui.
--
-- La logica e' invariata rispetto alla `0004`: stesso CASE, stesso coalesce a
-- 'free', stesso SQLSTATE dedicato. Cambia solo da dove viene letto il piano.

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
  -- Conta solo i creator **attivi**, cioe' quelli che il cron scraperebbe
  -- davvero. Contare lo storico bloccherebbe chi cancella e ricrea, o chi
  -- disattiva per fare spazio, senza che il costo lo giustifichi.
  --
  -- Per questo il controllo scatta anche sull'UPDATE, ma solo sulla
  -- transizione che *aumenta* gli attivi: senza, bastava creare al tetto,
  -- disattivare, creare altri, e infine riattivare tutti.
  if tg_op = 'INSERT' and not new.is_active then
    return new;
  end if;
  if tg_op = 'UPDATE' and not (new.is_active and not old.is_active) then
    return new;
  end if;

  select s.tier into piano
    from public.subscriptions s
   where s.user_id = new.user_id;

  -- `coalesce` copre la riga assente: si ricade sul piano piu' restrittivo, che
  -- e' il default sicuro se qualcosa e' andato storto a monte. Vale anche per
  -- gli utenti nuovi, per cui nessuno inserisce una riga finche' non arriva il
  -- billing: `handle_new_user` non e' stata estesa di proposito, per non
  -- aggiungere un secondo insert al percorso di signup — cioe' un modo in piu'
  -- di far fallire una registrazione — quando l'assenza della riga porta gia'
  -- allo stesso risultato.
  limite := case coalesce(piano, 'free')
              when 'pro' then 200
              else 30
            end;

  select count(*) into attuali
    from public.creators c
   where c.user_id = new.user_id
     and c.is_active
     and c.id <> new.id;

  if attuali >= limite then
    -- SQLSTATE dedicato: il backend lo riconosce e lo traduce in un messaggio
    -- pulito. Senza un codice proprio, questo errore finirebbe nel ramo
    -- generico e l'utente leggerebbe "servizio dati non disponibile" quando ha
    -- semplicemente raggiunto il limite del suo piano.
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

comment on function public.enforce_creator_limit() is
  'Limita i creator attivi per utente in base a subscriptions.tier (fino alla '
  '0006 leggeva profiles.subscription_tier). Il tetto vale per chiunque scriva, '
  'backend o client diretto via PostgREST: e'' il database l''arbitro, non il '
  'codice applicativo.';


-- -----------------------------------------------------------------------------
-- 4. La colonna vecchia viene RIMOSSA, non deprecata
-- -----------------------------------------------------------------------------
-- Lasciarla come colonna morta ricrearebbe esattamente il rischio che questa
-- migration elimina: due posti che sembrano entrambi la fonte di verita', e
-- qualcuno che un giorno scrive in quello sbagliato — senza che nulla fallisca,
-- perche' scrivere in una colonna inutilizzata riesce sempre.
--
-- Verificato prima di rimuoverla che nessun codice applicativo la legga o la
-- scriva: `git grep subscription_tier` su `backend/` e `frontend/` non
-- restituisce nulla. Le uniche due occorrenze di "profiles" nel codice sono un
-- campo di input di un actor Apify e la mappa generica di scoping in
-- `supabase_service.py`, nessuna delle due legata al piano.

alter table public.profiles drop column if exists subscription_tier;


-- -----------------------------------------------------------------------------
-- 5. Privilegi: nessuno, e non per dimenticanza
-- -----------------------------------------------------------------------------
-- `revoke all` copre anche TRUNCATE, che **non e' soggetto al RLS** — la lezione
-- della `0002`, dove una policy perfetta non avrebbe impedito a un client di
-- svuotare una tabella. Qui non viene concesso nulla fin dall'inizio, quindi la
-- revoca e' ridondante per costruzione: resta perche' un GRANT di default a
-- livello di schema, oggi o domani, non deve poter cambiare la conclusione.
--
-- RLS abilitato senza alcuna policy = nega tutto. Difesa in profondita': se un
-- GRANT venisse concesso per errore, la tabella non diventerebbe comunque
-- leggibile per quello solo.

alter table public.subscriptions enable row level security;

revoke all on public.subscriptions from anon, authenticated;

grant all on public.subscriptions to service_role;

-- NESSUN `grant select` ad `authenticated`, e non e' una svista: verificato che
-- oggi il piano non serva a nessuno lato client. Il frontend non nomina piani in
-- alcuna forma — nessun riferimento a piano/plan/tier/subscription/upgrade in
-- `frontend/` — e il backend non legge il piano se non dentro il trigger, che
-- gira come proprietario. Concedere una lettura che nessuno usa significa
-- ampliare la superficie in cambio di nulla.
--
-- QUANDO SERVIRA' MOSTRARE IL PIANO NELL'INTERFACCIA, bastano queste due righe —
-- e nient'altro: la scrittura resta preclusa comunque, perche' il GRANT concede
-- il solo SELECT e la policy copre il solo SELECT.
--
--     grant select on public.subscriptions to authenticated;
--
--     create policy "subscriptions_select_own" on public.subscriptions
--       for select to authenticated
--       using ((select auth.uid()) = user_id);
