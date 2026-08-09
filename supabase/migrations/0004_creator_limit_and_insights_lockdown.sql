-- =============================================================================
-- 0004 — Tetto ai creator per piano, e `insights` non piu' scrivibile dal client
-- =============================================================================
--
-- Nasce dall'audit di readiness del 9 agosto 2026, che ha misurato il costo
-- massimo generabile da un singolo account autenticato senza toccare alcun
-- endpoint del backend.
--
-- IL VETTORE, MISURATO
--
-- Un utente puo' scrivere su `creators` direttamente via PostgREST: possiede la
-- publishable key (che sta nel bundle del browser) e il proprio JWT. Misurato:
-- 186 insert riusciti in 15 secondi, cioe' 744/minuto, senza alcun rifiuto ne'
-- throttling, e tutte e 186 le righe rientravano nella SELECT del cron.
--
-- Il cron scrapa ogni creator attivo con `apify_results_per_creator = 10`
-- risultati. A $2,70/1.000 risultati (Instagram, free tier) un'ora di insert —
-- 44.640 creator — costa **$1.205 per singola esecuzione**, ripetuta a ogni
-- giro, per sempre. Nessun rate limiter applicativo lo intercetterebbe, perche'
-- quel percorso non passa dal backend.
--
-- PERCHE UN TETTO E NON UNA REVOCA
--
-- La revoca di INSERT/UPDATE/DELETE su `creators` — la strada usata nella 0002
-- per `profiles` — qui **romperebbe l'applicazione**: `POST`, `PATCH` e
-- `DELETE /api/v1/creators` scrivono con la chiave dell'utente
-- (`scoped_client`), non con la service role. Spostarli al bypass del RLS
-- contraddirebbe la scelta architetturale dichiarata nel README: dove esiste un
-- utente loggato, il RLS resta la rete di sicurezza.
--
-- Il problema del resto non e' che l'utente inserisca creator — e' il prodotto.
-- E' che non esisteva alcun limite, ne' sul percorso diretto ne' su quello
-- legittimo. Un tetto per utente li chiude entrambi.
--
-- IL NUMERO
--
-- Costo ricorrente per creator attivo, al giorno:
--   Apify   10 risultati x $0,0027 x 4 esecuzioni = $0,108
--   Gemini  ~1 video nuovo al giorno, 30s, tier caro = $0,032
--   totale  ~$0,140
--
-- Con 30 creator: ~$4,20 al giorno per utente al tetto massimo, sotto la
-- soglia di $5 fissata. Trenta creator monitorati sono un uso intenso ma
-- plausibile per una persona vera: il tetto non ostacola l'uso reale e rende
-- irrilevante l'abuso da un singolo account.
--
-- Il valore per `pro` e' un **segnaposto**: va deciso contro il prezzo del
-- piano, che non esiste ancora. A 200 creator il costo sale a ~$28/giorno per
-- utente, che deve stare sotto quanto quel piano incassa.
--
-- La struttura per piano esiste gia' da adesso: quando arrivera' il billing
-- bastera' che il backend scriva un `subscription_tier` diverso, senza nuove
-- migration.

-- -----------------------------------------------------------------------------
-- 1. `insights` non e' piu' scrivibile dal ruolo `authenticated`
-- -----------------------------------------------------------------------------
-- Verificato riga per riga prima di revocare: con la chiave dell'utente il
-- backend fa su `insights` **solo** SELECT (`GET /api/v1/insights`). Ogni
-- scrittura — l'upsert dell'analisi e quelle del cron — passa da
-- `service_table`, cioe' dalla service role, che ha `BYPASSRLS` e non e'
-- toccata da questa revoca. Il frontend non nomina mai la tabella: usa Supabase
-- solo per l'autenticazione.
--
-- Cosa chiude: un client poteva fabbricare righe di `insights` a piacere. Non
-- otteneva analisi gratis, ma qualunque quota o contatore di piano che domani
-- si basi sul numero di insight sarebbe stato scrivibile dal cliente stesso.

revoke insert, update, delete on public.insights from authenticated;


-- -----------------------------------------------------------------------------
-- 2. Tetto ai creator attivi, per piano
-- -----------------------------------------------------------------------------

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

  select p.subscription_tier into piano
    from public.profiles p
   where p.id = new.user_id;

  -- `coalesce` copre il profilo assente: si ricade sul piano piu' restrittivo,
  -- che e' il default sicuro se qualcosa e' andato storto a monte.
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
  'Limita i creator attivi per utente in base a profiles.subscription_tier. '
  'Il tetto vale per chiunque scriva, backend o client diretto via PostgREST: '
  'e'' il database l''arbitro, non il codice applicativo.';

-- `CREATE FUNCTION` concede EXECUTE a PUBLIC: qui non serve a nessuno se non al
-- trigger, che gira per conto del proprietario.
revoke execute on function public.enforce_creator_limit() from public;

drop trigger if exists creators_enforce_limit on public.creators;

create trigger creators_enforce_limit
  before insert or update on public.creators
  for each row
  execute function public.enforce_creator_limit();
