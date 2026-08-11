-- =============================================================================
-- 0009 — `cache_key`: l'identita' del video si separa dall'URL mostrato
-- =============================================================================
--
-- IL PROBLEMA (voce A9, punto 3 di SECURITY_AUDIT.md)
--
-- `UNIQUE (user_id, video_url)` faceva fare a una sola colonna due mestieri
-- incompatibili:
--
--   * **cosa si mostra** — `insights.video_url` finisce in un `href` cliccabile
--     (`insight-card.tsx`), quindi deve restare un URL che apre il video;
--   * **cosa identifica** — la chiave con cui si decide se un'analisi e' gia'
--     stata pagata.
--
-- Il secondo mestiere vorrebbe unificare piu' del primo. Lo stesso video TikTok
-- e' raggiungibile con **username diversi** nel path: `tiktok.com/@tizio/video/123`
-- e `tiktok.com/@caio/video/123` aprono entrambi il video `123`. Come URL da
-- mostrare sono due valori legittimi e distinti; come identita' sono lo stesso
-- video — e finche' la chiave era l'URL, si pagavano due analisi.
--
-- Da qui la colonna nuova. `video_url` torna a fare **solo** il primo mestiere.
--
-- PERCHE NON `NULL` PER GLI URL SENZA ID
--
-- Un link diretto a un `.mp4` non ha un id di piattaforma da estrarre. Lasciare
-- `cache_key` a `NULL` in quel caso sembrerebbe naturale e sarebbe un errore:
-- in PostgreSQL **due `NULL` non collidono**, quindi un vincolo di unicita' su
-- una colonna nullable non deduplica proprio le righe che la lasciano vuota. Si
-- ricade quindi sull'URL normalizzato, che per quel caso e' la migliore identita'
-- disponibile — ed e' esattamente cio' che la chiave era prima.
--
-- LA CHIAVE DEVE ESSERE LA STESSA IN QUATTRO PUNTI
--
-- Cache di lettura, target dell'upsert, lock sulle analisi concorrenti e dedup
-- del cron. **Se anche uno solo usasse un valore diverso**, due richieste sullo
-- stesso video passerebbero per chiavi diverse e la spesa tornerebbe doppia — il
-- difetto che la `0003` aveva chiuso, riaperto dalla porta di servizio. Per
-- questo la migration ri-chiave anche `analysis_locks`, che altrimenti
-- arbitrerebbe su una chiave diversa da quella che deduplica.

-- -----------------------------------------------------------------------------
-- 1. La colonna, e il backfill
-- -----------------------------------------------------------------------------

alter table public.insights add column if not exists cache_key text;

-- Le espressioni qui sotto rispecchiano `_PIATTAFORME` di `media_service.py`
-- **al momento di questa migration**, e sono deliberatamente una copia una
-- tantum, non una seconda fonte di verita': servono solo alle righe gia'
-- esistenti. Da qui in avanti il valore lo scrive sempre `canonical_cache_key`
-- in Python, e su un database nuovo questo `update` non tocca alcuna riga.
--
-- Possono assumere la forma normalizzata (`https`, host senza `www.`, senza
-- slash finale) perche' ogni riga in `insights` e' stata scritta passando da
-- `normalize_video_url`.
update public.insights
   set cache_key = case
         when video_url ~ '^https://instagram\.com/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+$'
           then 'instagram.com:' || substring(
                  video_url from '^https://instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)$')
         when video_url ~ '^https://tiktok\.com/@[^/]+/video/[0-9]+$'
           then 'tiktok.com:' || substring(
                  video_url from '^https://tiktok\.com/@[^/]+/video/([0-9]+)$')
         else video_url
       end
 where cache_key is null;


-- -----------------------------------------------------------------------------
-- 2. Il vincolo si sposta
-- -----------------------------------------------------------------------------
-- `NOT NULL` va messo **dopo** il backfill e dopo aver verificato che nessuna
-- coppia `(user_id, cache_key)` collida: due righe che collassano sulla stessa
-- chiave farebbero fallire l'indice, e il momento per accorgersene e' prima di
-- eseguire questo blocco, non durante.

alter table public.insights alter column cache_key set not null;

comment on column public.insights.cache_key is
  'Identita'' del video ai fini della deduplica: `<host>:<id>` quando la '
  'piattaforma e'' nota, altrimenti l''URL normalizzato. **Non e'' un URL e non '
  'va mostrata**: per il link si usa `video_url`. La scrive '
  'media_service.canonical_cache_key.';

-- Il vincolo vecchio non esprime piu' l'intenzione: due forme dello stesso video
-- hanno `video_url` diversi ed e' corretto che li abbiano. Toglierlo non allenta
-- nulla, perche' `video_url` e' una funzione di `cache_key` piu' la forma
-- ricevuta, e la deduplica ora vive sulla seconda.
alter table public.insights drop constraint if exists insights_user_id_video_url_key;

alter table public.insights
  add constraint insights_user_id_cache_key_key unique (user_id, cache_key);


-- -----------------------------------------------------------------------------
-- 3. `analysis_locks` arbitra sulla stessa chiave
-- -----------------------------------------------------------------------------
-- **Non e' un dettaglio di coerenza: senza, la 0009 riaprirebbe la spesa
-- duplicata.** Con la cache su `cache_key` e il lock su `video_url`, due
-- richieste concorrenti sullo stesso video con username diversi otterrebbero
-- **lock distinti**, procederebbero entrambe, pagherebbero entrambe Gemini e
-- Apify, e solo alla scrittura scoprirebbero di essere la stessa riga. Cioe'
-- esattamente il difetto che la `0003` esiste per chiudere, sui casi che la 0009
-- introduce.
--
-- Un `rename` e non una colonna nuova: la primary key, l'indice e i privilegi
-- seguono automaticamente, e le righe presenti sono lock in corso con TTL — al
-- massimo un'analisi in volo attende la scadenza. Il **meccanismo non cambia**:
-- restano i due passi atomici (INSERT, e in caso di collisione un
-- `UPDATE ... WHERE expires_at <= now()` che sottrae solo lo scaduto) e lo stesso
-- TTL. Cambia la stringa che fa da chiave.

alter table public.analysis_locks rename column video_url to cache_key;

comment on column public.analysis_locks.cache_key is
  'Stessa chiave di insights.cache_key. Deve restare identica: se il lock '
  'arbitrasse su un valore diverso da quello che deduplica, due richieste sullo '
  'stesso video otterrebbero lock distinti e pagherebbero entrambe.';

comment on table public.analysis_locks is
  'Lock a scadenza sulle analisi in corso, su (user_id, cache_key, '
  'analysis_mode). Nessuno lo legge o scrive se non il backend col client '
  'service-role: evita di pagare due volte Apify e Gemini per lo stesso video.';
