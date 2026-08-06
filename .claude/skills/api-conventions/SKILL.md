---
name: api-conventions
description: Convenzioni degli endpoint FastAPI di Picox — 422 per validazione fallita, 503 per fallimenti Gemini/Apify senza propagare stack trace, blocco try/except globale a livello di router, response_model esplicito su ogni endpoint. Usare ogni volta che si aggiunge o modifica una route in backend/app/api/ o si tocca backend/app/middleware/error_handler.py.
---

# Convenzioni API — Picox

## 1. Mappa degli status code

| Situazione | Status | Chi lo produce |
| --- | --- | --- |
| Body/query non valido, video oltre i limiti di dimensione o durata | `422` | `RequestValidationError`, `ValidationError`, `PicoxValidationError` |
| JWT assente, scaduto o non verificabile; `X-CRON-SECRET` errato | `401` | `AuthError` |
| Risorsa non appartenente all'utente o inesistente | `404` | `NotFoundError` |
| Violazione di unicità (`user_id, username, platform`) | `409` | `ConflictError` |
| Gemini o Apify falliscono, vanno in rate-limit o restituiscono output non parsabile | `503` | `ExternalServiceError` |
| Qualsiasi eccezione non prevista | `500` | handler generico |

`503` e non `500`: i fallimenti dei provider esterni sono transitori e il client
può ritentare. `500` significa "bug nostro" e va tenuto raro e visibile.

## 2. Nessuno stack trace nella risposta

Il body di errore ha sempre e solo questa forma:

```json
{ "error": { "code": "external_service_error", "message": "…", "details": … } }
```

- Il `message` è leggibile da un utente finale e **non contiene** nomi di provider
  con credenziali, URL interni, query SQL o frammenti di traceback.
- Il traceback completo si logga lato server con `logger.exception(...)`, mai si
  serializza nella risposta.
- Le eccezioni dei client esterni non vengono ri-lanciate così come sono: si
  incapsulano in `ExternalServiceError` con un messaggio scritto da noi. Il testo
  dell'eccezione originale resta nei log.

## 3. Blocco try/except globale per router

Ogni `APIRouter` usa `route_class=SafeRoute` (`app/middleware/error_handler.py`).
`SafeRoute` avvolge l'handler in un try/except unico che:

- lascia passare `HTTPException` e le eccezioni di dominio Picox (gli handler
  registrati sull'app le mappano sugli status della tabella sopra);
- converte **qualsiasi** altra eccezione in `500` sanitizzato, dopo averla loggata
  con lo stack trace completo.

```python
router = APIRouter(prefix="/api/v1", tags=["insights"], route_class=SafeRoute)
```

Conseguenza pratica: dentro le route si scrive il percorso felice e si sollevano
eccezioni di dominio. Niente `try/except Exception` sparsi per catturare l'ignoto —
c'è già la rete a livello di router. Un `try/except` locale si giustifica solo per
tradurre un errore specifico in un'eccezione di dominio più precisa
(es. `APIError` con `code == "23505"` → `ConflictError`).

## 4. `response_model` esplicito su ogni endpoint

```python
@router.get("/insights", response_model=InsightListResponse)
async def list_insights(...) -> InsightListResponse:
    ...
```

- **Sempre** `response_model=...`, anche quando il return type annotation già lo
  dice: è ciò che entra nell'OpenAPI su cui il frontend genera i tipi.
- Lo schema di risposta è un modello Pydantic dedicato, mai `dict` o `Any`. Vale da
  filtro d'uscita: un campo non dichiarato nel modello non raggiunge il client,
  ed è la difesa contro il leak accidentale di colonne interne.
- `status_code=` esplicito quando non è `200` (`201` sulle create, `204` sulle
  delete).
- Dichiarare le risposte d'errore rilevanti in `responses={...}` così che finiscano
  nell'OpenAPI.

## 5. Autenticazione

- Ogni route eccetto `GET /health` dipende da `get_current_user`. Nessuna route
  "temporaneamente aperta".
- `user_id` arriva **solo** dal JWT verificato (`user.id`). Mai dal body, mai dalla
  query string, mai da un path param: un `user_id` accettato dall'esterno è un IDOR.
- Le route cron dipendono da `verify_cron_secret` (header `X-CRON-SECRET`,
  confronto in tempo costante) e ricavano gli `user_id` da `creators`.

## 6. Checklist prima del commit

- [ ] `response_model` su ogni endpoint, con un modello Pydantic dedicato.
- [ ] Router creato con `route_class=SafeRoute`.
- [ ] Nessun `raise HTTPException(500, str(e))` — usare le eccezioni di dominio.
- [ ] Nessun `user_id` letto dalla request.
- [ ] Fallimenti Gemini/Apify → `ExternalServiceError` (`503`), con retry singolo
      dove ha senso.
- [ ] Nessuna chiave, URL interno o traceback nel body di risposta.
