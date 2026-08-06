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
uvicorn app.main:app --reload
```

`http://localhost:8000/docs` per l'OpenAPI interattivo (disattivato quando
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
    creators.py              CRUD dei creator
    insights.py              feed, filtri, riepilogo del cron
  services/
    supabase_service.py      i due client + scoping obbligatorio del service-role
    gemini_service.py        upload, inferenza, validazione, cleanup
    apify_service.py         scraping per piattaforma, normalizzato
    media_service.py         normalizzazione URL, download con limiti
  api/v1/
    analyze.py               POST /analyze-video (+ pipeline riusata dal cron)
    creators.py              CRUD
    insights.py              GET /insights
    cron.py                  POST /cron/check-updates
  middleware/
    error_handler.py         SafeRoute + handler globali
prompts/                     prompt Gemini per modalità (base, info, style, script)
```

## Endpoint

| Metodo | Path | Auth | Note |
| --- | --- | --- | --- |
| `GET` | `/health` | — | `{"status":"ok"}` |
| `POST` | `/api/v1/analyze-video` | JWT | `201` se analizzato, `200` se già in cache |
| `POST` | `/api/v1/creators` | JWT | `409` se già monitorato |
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

- **Rate limiting su `POST /analyze-video`.** È l'endpoint più costoso (download
  fino a 200 MB più un'inferenza multimodale) ed è protetto solo dal JWT: un
  utente autenticato può accodare analisi illimitate. Serve un limite per utente
  o un contatore giornaliero su `profiles`.
- **DNS rebinding.** La guard sul download valida l'IP risolto a ogni hop, ma non
  fissa la connessione su quell'IP: una risposta DNS che cambia fra controllo e
  connessione resta teoricamente sfruttabile.

## Pipeline di analisi

1. normalizzazione dell'URL — è la chiave di `UNIQUE (user_id, video_url)`, e
   senza di essa lo stesso Reel con un `?igshid=` diverso viene pagato due volte;
2. cache check su `(user_id, video_url)`: se c'è, si restituisce il record e
   nessun servizio esterno viene chiamato;
3. risoluzione dei metadati via Apify (best effort: un fallimento non è fatale);
4. download in un file temporaneo, interrotto appena si supera il limite di
   dimensione; `422` se dimensione o durata eccedono. I redirect sono seguiti a
   mano e **ogni hop** viene validato: un URL che risolve su un indirizzo non
   pubblico viene rifiutato, altrimenti l'endpoint sarebbe un SSRF verso la rete
   interna del container (`169.254.169.254`, `127.0.0.1`, RFC1918);
5. inferenza Gemini con `response_schema=VideoAnalysisResponse`, prompt composto
   dalle sole sezioni pertinenti alla modalità;
6. validazione Pydantic con **un retry**, poi `503` — sul database non si scrive
   nulla di non validato;
7. `upsert` su `insights` con `on_conflict=user_id,video_url`, che assorbe anche
   due richieste concorrenti sullo stesso video.

File temporaneo locale e file remoto sulla Gemini File API vengono cancellati in
`finally`, anche quando l'inferenza fallisce.

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
uvicorn app.main:app --reload    # e poi: curl localhost:8000/health
```
