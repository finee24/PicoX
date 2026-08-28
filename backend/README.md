# backend — API e pipeline di analisi

FastAPI + Pydantic v2. Analizza video brevi con Gemini in structured output,
recupera i nuovi video dei creator monitorati via Apify e persiste tutto su
Supabase.

---

## Avvio locale

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate     # Windows
# python3 -m venv .venv && source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
cp .env.example .env      # e compilare
./dev.ps1                 # bash/zsh: ./dev.sh
```

`dev.ps1` fissa interprete e porta: senza, si riscrivono a mano ogni volta ed è
lì che si sbagliano. **La porta è la 8001**, non la 8000 di default di uvicorn —
è quella su cui il frontend cerca il backend.

`http://localhost:8001/docs` per l'OpenAPI interattivo (disattivato quando
`ENVIRONMENT=production`).

`ffprobe` (pacchetto `ffmpeg`) è opzionale in locale: senza, il limite di durata
non viene verificato e resta attivo il solo limite di dimensione. Nell'immagine
Docker è incluso.

## Docker

```bash
docker build -t picox-backend .
docker run --rm -p 8000:8000 --env-file .env picox-backend
```

Su Render/Railway la porta arriva da `$PORT`: il `CMD` la legge già.
Health check da puntare su `/health`.

---

## Struttura

```
app/
  main.py                    app factory, CORS, /health
  core/
    config.py                Settings (pydantic-settings); i segreti sono SecretStr
    security.py              verifica JWT Supabase (HS256 o JWKS) e X-CRON-SECRET
    exceptions.py            eccezioni di dominio, ognuna con il proprio status
  schemas/
    analysis.py              InfoAnalysis, StyleAnalysis, InverseScript, VideoAnalysisResponse
    creators.py              CRUD dei creator, validazione di un account
    insights.py              feed, filtri, riepilogo del cron
    scraping.py              ScraperResult: l'interfaccia comune degli scraper
  services/
    supabase_service.py      i due client + scoping obbligatorio del service-role
    gemini_service.py        upload o passthrough, inferenza, validazione, cleanup
    apify_service.py         scraping per piattaforma, normalizzato
    youtube_service.py       Data API v3: channels.list e videos.list
    content_scraper.py       uno scraper per piattaforma, un solo ScraperResult
    creator_validation.py    esiste + è pubblico: parsing, cache, quota
    prompt_loader.py         compone il prompt dal YAML + sezioni per modalità
    media_service.py         normalizzazione URL, download con limiti
  api/v1/
    analyze.py               POST /analyze-video (+ pipeline riusata dal cron)
    creators.py              CRUD + POST /creators/validate
    insights.py              GET /insights
    cron.py                  POST /cron/check-updates
  middleware/
    error_handler.py         SafeRoute + handler globali
prompts/
  analysis_prompt.yaml       base, override per piattaforma, blocco di contesto
  info.md style.md script.md sezioni innestate secondo la modalità richiesta
```

### Il prompt si edita senza toccare il codice

`prompts/analysis_prompt.yaml` tiene la parte che cambia spesso; le sezioni per
modalità restano `.md` perché cambiano per conto proprio.
`prompt_loader.build_analysis_prompt` le compone. Il file è letto una volta e
tenuto in cache: dopo una modifica va riavviato il processo.

**La forma della risposta non si descrive lì.** La impone
`response_schema=VideoAnalysisResponse`, che `google-genai` converte in JSON
Schema e invia insieme al video. Riscrivere lo schema nel prompt significa
darne due al modello: quando divergono la risposta smette di validare e
l'analisi finisce in `503` — su tutte le richieste, non su qualcuna.
Nel YAML si scrive il criterio, non il contratto.

`user_override_base_prompt` è predisposto per una futura personalizzazione per
utente (un `base_prompt` su Supabase) e non è ancora usato. Sostituisce **solo**
la base: modalità, override di piattaforma e delimitazione del contesto non sono
personalizzabili, perché sono ciò che tiene la risposta dentro lo schema.

## Endpoint

| Metodo | Path | Auth | Note |
| --- | --- | --- | --- |
| `GET` | `/health` | — | `{"status":"ok"}` |
| `POST` | `/api/v1/analyze-video` | JWT | `201` se analizzato, `200` se già in cache |
| `POST` | `/api/v1/creators` | JWT | `409` se già monitorato |
| `POST` | `/api/v1/creators/validate` | JWT | esiste + è pubblico; cache 24h, quota giornaliera |
| `GET` | `/api/v1/creators` | JWT | |
| `PATCH` | `/api/v1/creators/{id}` | JWT | `analysis_mode`, `is_active` |
| `DELETE` | `/api/v1/creators/{id}` | JWT | `204`; gli insight sopravvivono |
| `GET` | `/api/v1/insights` | JWT | `search`, `mode`, `creator_id`, `page`, `limit` |
| `POST` | `/api/v1/cron/check-updates` | `X-CRON-SECRET` | analisi in background |

Errori, forma unica:

```json
{ "error": { "code": "video_too_large", "message": "…", "details": … } }
```

`401` auth · `404` non trovato · `409` conflitto · `422` validazione e limiti
video · `503` Gemini/Apify/database · `500` imprevisto. Mai uno stack trace nel
corpo della risposta.

---

## Le due chiavi Supabase

Il punto più delicato del backend. `SUPABASE_SERVICE_ROLE_KEY` **bypassa il Row
Level Security**: con quel client il database non separa più i tenant.

**Client scoped al JWT** (`scoped_client`) — anon key + JWT dell'utente. Ruolo
`authenticated`, RLS attivo. È il default: letture del feed, CRUD dei creator,
cache lookup. Un filtro dimenticato qui produce zero righe, non una fuga di dati.

**Client service-role** (`get_service_client`) — solo dove un JWT non esiste o
non è più affidabile: la scrittura di `insights` dopo l'inferenza e il job cron.

Lo scoping non è affidato alla disciplina di chi scrive la query: il client
service-role si usa attraverso `service_table(client, tabella, user_id)`, che
applica da sé `.eq('user_id', ...)` a ogni SELECT/UPDATE/DELETE e forza il
proprietario in ogni INSERT/UPSERT. Un `user_id` vuoto solleva `RuntimeError`
invece di produrre una query su tutti i tenant.

L'unica eccezione è `unscoped_service_table(..., reason=...)`, che pretende una
motivazione e la registra nei log: la usa solo il cron per enumerare i creator
attivi, perché lì un utente corrente non esiste. Da quel punto in poi ogni
operazione riparte dallo `user_id` letto sulla riga del creator.

`user_id` viene **sempre** dal JWT verificato o da una riga del database, mai dal
body, dalla query string o da un path param.

## Cosa manca prima di aprire al pubblico

- ~~**Rate limiting su `POST /analyze-video`.**~~ Chiuso dalla migration `0008`:
  quota giornaliera per piano, imposta dal trigger su `analysis_events`. Stesso
  meccanismo sulla validazione dei creator (`0010`).
- **DNS rebinding.** La guard sul download valida l'IP risolto a ogni hop, ma non
  fissa la connessione su quell'IP: una risposta DNS che cambia fra controllo e
  connessione resta teoricamente sfruttabile.

## Pipeline di analisi

1. normalizzazione dell'URL — è ciò da cui si deriva `cache_key`, e senza di essa
   lo stesso Reel con un `?igshid=` diverso viene pagato due volte;
2. cache check su `(user_id, cache_key)`: se c'è, si restituisce il record e
   nessun servizio esterno viene chiamato;
3. **scraping** (`content_scraper.py`): uno scraper per piattaforma, un solo
   `ScraperResult` in uscita. Best effort — un fallimento non è fatale, si tenta
   comunque l'URL così com'è;
4. **un solo bivio a valle** (`run_analysis`), su `youtube_url` contro
   `video_bytes_url`:
   - *passthrough* (YouTube) — l'URL va a Gemini in `file_data` e a scaricarlo è
     Google. Nessun byte da noi, nessun file remoto da cancellare. Si usa solo se
     `videos.list` ha confermato una durata entro il limite: senza download, è
     l'unico momento in cui `MAX_VIDEO_DURATION_SECONDS` può essere applicato;
   - *byte* (Instagram, TikTok, link diretti) — download in un file temporaneo,
     interrotto appena si supera il limite di dimensione, poi upload sulla Files
     API. `422` se dimensione o durata eccedono. I redirect sono seguiti a mano e
     **ogni hop** viene validato: un URL che risolve su un indirizzo non pubblico
     viene rifiutato, altrimenti l'endpoint sarebbe un SSRF verso la rete interna
     del container (`169.254.169.254`, `127.0.0.1`, RFC1918);
5. inferenza Gemini con `response_schema=VideoAnalysisResponse`, prompt composto
   da `prompt_loader` con le sole sezioni pertinenti alla modalità, più le
   istruzioni della piattaforma e il blocco di contesto (autore, caption,
   hashtag) — **delimitato come dato non fidato**, perché lo scrive il creator
   del video e non l'utente di Picox. `fps` e `media_resolution` arrivano dal
   preset della modalità: `INFO` campiona meno frame e a risoluzione più bassa,
   `STYLE` e `BOTH` restano pieni perché misurano il montaggio;
6. validazione Pydantic con **un retry**, poi `503` — sul database non si scrive
   nulla di non validato;
7. `upsert` su `insights` con `on_conflict=user_id,cache_key`, che assorbe anche
   due richieste concorrenti sullo stesso video.

Sul percorso con download, file temporaneo locale e file remoto sulla Gemini
Files API vengono cancellati in `finally`, anche quando l'inferenza fallisce. Sul
passthrough non c'è nulla da cancellare.

## Ricerca (`GET /api/v1/insights`)

`search` combina `keywords @> {termine}` (indice GIN, corrispondenza esatta della
keyword) e `ilike` sui campi testuali di `summary_data`. Il termine è ripulito
dai metacaratteri del parser PostgREST prima di entrare nel filtro.

Quando la tabella cresce, il passo successivo è una colonna `tsvector` generata
su `summary_data` + `keywords` con indice GIN, e un solo `text_search` al posto
dell'`or`.

`mode=SCRIPT` non filtra su `analysis_mode`: seleziona i record con
`inverse_script_template IS NOT NULL`, a prescindere dalla modalità con cui sono
stati generati.

---

## Convenzioni

Codificate come skill di progetto, caricate automaticamente da Claude Code:

- `.claude/skills/api-conventions/SKILL.md` — status code, forma degli errori,
  `SafeRoute`, `response_model` obbligatorio;
- `.claude/skills/gemini-structured-output/SKILL.md` — `Field(description=...)`
  su ogni campo, validazione prima della scrittura, guard e cleanup.

Prima di ogni commit: `.claude/agents/security-reviewer.md`. Per iterare sui
prompt senza trascinare il resto del backend nel contesto:
`.claude/agents/prompt-tuner.md`.

## Verifiche

```bash
pyright                          # 0 errori sul modulo app/
./dev.ps1                        # e poi: curl localhost:8001/health
```
