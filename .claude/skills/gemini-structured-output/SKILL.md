---
name: gemini-structured-output
description: Regole per gli schemi Pydantic v2 passati a Gemini come response_schema in Picox — Field(description=...) obbligatorio su ogni campo, validazione con .model_validate() prima di scrivere su Supabase, guard su dimensione/durata del video prima dell'upload con fallback 503. Usare ogni volta che si scrive o modifica backend/app/schemas/analysis.py, backend/app/services/gemini_service.py o un prompt in backend/prompts/.
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

## 6. Checklist prima del commit

- [ ] Ogni campo di ogni modello passato come `response_schema` ha `description`.
- [ ] Nessun `dict[str, Any]` / `Any` nei modelli di risposta.
- [ ] `.model_validate()` / `.model_validate_json()` chiamato prima di ogni scrittura.
- [ ] Retry singolo sul parsing, poi `503`.
- [ ] Limiti di dimensione/durata letti da `Settings` e applicati prima dell'upload.
- [ ] `finally` che cancella file locale **e** file remoto.
- [ ] Nessuna `GEMINI_API_KEY` nei log o nei messaggi d'errore.
