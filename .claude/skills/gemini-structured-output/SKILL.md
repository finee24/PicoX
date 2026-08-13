---
name: gemini-structured-output
description: Regole per gli schemi Pydantic v2 passati a Gemini come response_schema in Picox — Field(description=...) obbligatorio su ogni campo, validazione con .model_validate() prima di scrivere su Supabase, guard su dimensione/durata del video prima dell'upload con fallback 503, e il divieto di riscrivere lo schema dentro il prompt. Usare ogni volta che si scrive o modifica backend/app/schemas/analysis.py, backend/app/services/gemini_service.py, backend/app/services/prompt_loader.py o un prompt in backend/prompts/ (analysis_prompt.yaml incluso).
---

# Gemini structured output — regole di progetto

Vale per ogni chiamata a `google-genai` che usa `response_mime_type="application/json"`
e `response_schema=<ModelloPydantic>`.

## 1. Lo schema è il prompt

Gemini riceve **lo schema JSON derivato dal modello Pydantic**, descrizioni incluse.
Un campo senza descrizione è un campo che il modello riempie a caso.

- **Ogni** campo ha un `Field(description=...)` esplicito. Nessuna eccezione, nemmeno
  per i campi "ovvi" come `title` o `summary`.
- La descrizione dice *cosa contenere e in che forma*, non ripete il nome del campo:

  ```python
  # NO — non aggiunge informazione
  hook: str = Field(description="L'hook del video")

  # OK — vincola contenuto, lingua e formato
  hook: str = Field(
      description=(
          "Trascrizione letterale delle parole pronunciate o mostrate nei primi "
          "3 secondi, che catturano l'attenzione. Lingua originale del video."
      )
  )
  ```
- I vincoli numerici e di lunghezza vanno nel `Field` (`ge`, `le`, `min_length`,
  `max_length`), non solo nella descrizione: finiscono nello schema e sono
  rivalidati da Pydantic dopo la risposta.
- Per gli enum usare `Literal[...]`: produce un `enum` nello schema e impedisce
  al modello di inventare valori.

## 2. Niente `dict` liberi, niente `Any`

Il payload finisce in colonne `jsonb` di Supabase. Lo schema tipizzato è l'unico
punto in cui la forma è verificata.

- Struttura annidata → sotto-modello Pydantic, mai `dict[str, Any]`.
- Liste → `list[SubModel]` o `list[str]`, sempre con descrizione del criterio di
  ordinamento e del numero atteso di elementi.
- Campo opzionale → `T | None = Field(default=None, description=...)`. Serve per i
  prompt a modalità (`INFO` / `STYLE` / `BOTH`), dove le sezioni non richieste
  devono tornare `null` invece di essere allucinate.

## 3. Validare prima di scrivere su Supabase

Mai passare `response.text` o `response.parsed` direttamente al database.

```python
raw = response.text
analysis = VideoAnalysisResponse.model_validate_json(raw)   # <- obbligatorio
payload = analysis.model_dump(mode="json")
```

- La validazione avviene **prima** di qualsiasi `insert`/`upsert`.
- `ValidationError` → **un solo retry** della chiamata a Gemini (il modello a volte
  tronca il JSON). Se anche il retry fallisce → `503`, e sul database non si scrive
  nulla.
- Serializzare con `model_dump(mode="json")`: `datetime`/`UUID`/`Enum` devono
  diventare tipi JSON nativi prima di finire in `jsonb`. **Senza**
  `exclude_none=True`: la forma del `jsonb` deve restare identica fra un record e
  l'altro, così il frontend distingue "campo assente nel video" da "campo che non
  esisteva nello schema di allora".
- Non loggare mai `raw` per intero in produzione: può contenere trascrizioni del
  video. Loggare lunghezza ed errore di validazione.

## 4. Guard su dimensione e durata *prima* dell'upload

L'upload su File API e l'inferenza sono le operazioni costose. Il controllo va
fatto **prima**, non dopo.

- Limiti da `Settings` (`MAX_VIDEO_MB`, `MAX_VIDEO_DURATION_SECONDS`), mai
  hardcoded nel service.
- Verificare il `Content-Length` in fase di `HEAD`/streaming e **interrompere il
  download** appena la soglia è superata: non si scarica un file da 2 GB per poi
  scoprire che è fuori limite.
- Video oltre i limiti → `422` con un messaggio che riporta limite e valore
  misurato. È un errore dell'input, non del servizio.
- Se il probe della durata non è disponibile (nessun `ffprobe`, nessun metadato
  dallo scraper) si procede con il solo limite di dimensione e si logga a `WARNING`:
  la guard mancante non deve bloccare l'analisi.
- Fallimento dell'upload, del passaggio in stato `ACTIVE` o della generazione →
  `503`, mai `500`, e mai con lo stack trace nella risposta.

## 5. Cleanup garantito

Due risorse da liberare, entrambe in `finally`:

1. il file temporaneo locale (`os.unlink`);
2. il file remoto sulla Gemini File API (`client.files.delete(name=...)`).

Il cleanup avviene anche quando l'inferenza fallisce o va in timeout. Un errore
durante il cleanup si logga e **non** si propaga: non deve mascherare l'errore
originale né trasformare un successo in un `500`.

## 6. Il percorso passthrough (solo YouTube)

Un URL YouTube si passa a Gemini in `file_data` e a scaricarlo è Google. Cambia
**cosa vale delle due sezioni precedenti**:

- niente upload e niente file remoto → il `finally` della sezione 5 non ha nulla
  da cancellare, e infatti `analyze_video_url` non ne ha uno;
- niente download → **la guard della sezione 4 non può misurare nulla**. La
  durata deve arrivare dai metadati (`videos.list`) *prima* della chiamata, e se
  non arriva non si fa passthrough: si ricade sul percorso con download, che il
  limite lo applica davvero. Un passthrough incondizionato equivale a non avere
  `MAX_VIDEO_DURATION_SECONDS` su quella piattaforma.
- il `mime_type` **non** si dichiara: il contenuto non è un file caricato da noi.

Vale solo per YouTube: è l'unica sorgente che il modello risolve da sé.

## 7. Ridurre i token senza pre-elaborare il video

Niente ffmpeg, niente estrazione di frame lato nostro. Due leve native, che si
sommano e si impostano nella stessa chiamata:

- `video_metadata.fps` sul Part — quanti frame al secondo. Costo **lineare**:
  metà frame rate, metà token dei frame. Default dell'API 1.0.
- `media_resolution` sulla `GenerateContentConfig` (per richiesta) — quanto vale
  un frame: ~258 token a media risoluzione, ~66 a `LOW`.

Il valore si sceglie dalla **modalità di analisi già richiesta dall'utente**, non
da una costante globale: `STYLE` e `BOTH` misurano il montaggio e restano a
frame rate pieno, `INFO` no. Per lo short-form non scendere sotto 0.3-0.5 FPS:
i tagli rapidi rendono invisibili i beat visivi a campionamenti più radi.

## 8. Il prompt non descrive lo schema

Il prompt sta in `backend/prompts/analysis_prompt.yaml` e lo compone
`prompt_loader.build_analysis_prompt`. Una regola sola, ma assoluta:

**Nel prompt non si scrive un elenco di campi.** Lo schema arriva già al modello
da `response_schema`, e un secondo elenco nel testo è un secondo schema. Quando i
due divergono il modello segue quello sbagliato, `model_validate_json` fallisce,
il retry fallisce con lo stesso prompt, e l'analisi finisce in `503` — su *tutte*
le richieste, non su qualcuna. Nel file si scrive il criterio e la profondità;
la forma è il modello Pydantic.

Corollario: un vocabolario chiuso si nomina, non si ricopia. La `Literal` è già
la fonte, e il loader la legge da lì (`get_args`); una lista riscritta a mano nel
YAML è libera di divergere, e quando diverge il prompt chiede valori che la
validazione rifiuta.

### Il testo che accompagna il video non è nostro

Caption, hashtag e nome dell'autore li scrive il creator del contenuto
scrapato — non l'utente autenticato — e finiscono nello stesso prompt delle
istruzioni. Tre proprietà li tengono a bada:

- il blocco è **delimitato in apertura e in chiusura**, con marcatori diversi da
  quelli che separano le sezioni, e dopo la chiusura una riga rimette l'ultima
  parola al sistema. La caption è l'ultimo testo del prompt: è la posizione da
  cui un'iniezione peserebbe di più;
- i marcatori sono **irriproducibili dal testo che delimitano**. Stanno in un
  file del repo, quindi sono pubblici: senza neutralizzare le sequenze di `=`
  nei valori, una caption che riscrive la riga di chiusura chiude il blocco in
  anticipo e da lì in poi parla al modello dalla posizione del sistema. Un
  delimitatore che il dato può falsificare è decorazione;
- la sostituzione avviene con `str.format` **sul template**, mai sui valori: una
  caption che contiene `{...}` resta testo e non diventa un segnaposto da
  risolvere. È il motivo per cui non serve sanificarla oltre;
- la caption viene troncata prima di entrare nel prompt: non contro il creator,
  contro uno scraper che restituisca un blob al posto di un testo.

## 9. Checklist prima del commit

- [ ] Ogni campo di ogni modello passato come `response_schema` ha `description`.
- [ ] Nessun `dict[str, Any]` / `Any` nei modelli di risposta.
- [ ] `.model_validate()` / `.model_validate_json()` chiamato prima di ogni scrittura.
- [ ] Retry singolo sul parsing, poi `503`.
- [ ] Limiti di dimensione/durata letti da `Settings` e applicati prima dell'upload.
- [ ] Sul passthrough: durata verificata dai metadati, altrimenti niente passthrough.
- [ ] `finally` che cancella file locale **e** file remoto (percorso con upload).
- [ ] Nessun elenco di campi nel prompt: lo schema lo fornisce `response_schema`.
- [ ] Caption e hashtag dentro i delimitatori, con la chiusura e la riga che segue.
- [ ] Ogni valore di terzi neutralizzato (marcatori, caratteri di controllo) e
      limitato in lunghezza — hashtag e nome dell'autore compresi, non solo la caption.
- [ ] Nessuna `GEMINI_API_KEY` nei log o nei messaggi d'errore.
