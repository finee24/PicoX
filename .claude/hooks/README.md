# Hook di enforcement

Quattro controlli deterministici configurati in `.claude/settings.json`. Sono
*deterministici* di proposito: non chiedono a un modello di ricordarsi una
regola, la applicano.

| Hook | Quando | Cosa fa |
|---|---|---|
| `block_frontend_secrets.py` | `PreToolUse` su Write/Edit | Blocca ogni scrittura in `frontend/` che contenga `GEMINI_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY` o il *valore* di una credenziale nota. |
| `lint_python.py` | `PostToolUse` su `.py` | `ruff check` sul file toccato + `mypy` sul progetto, riportando solo le righe di quel file. |
| `lint_frontend.py` | `PostToolUse` su `.ts`/`.tsx` | `eslint --fix`; segnala solo ciò che non è correggibile in automatico. |
| `require_tests.py` | `Stop` | Se ci sono modifiche non committate sotto `backend/`, non lascia chiudere il turno finché `pytest` non passa. |

Convenzione del protocollo: JSON su stdin, **uscita 2** per bloccare, testo su
stderr che torna al modello. Ogni altra uscita non blocca.

## Scelte non ovvie

**Il blocco dei segreti cerca anche i valori, non solo i nomi.** Un controllo
sui soli identificatori si aggira incollando la chiave in una costante senza mai
nominarla. I pattern coprono i token Apify, le chiavi Google (`AIza` e `AQ.`) e
la chiave `service_role` di Supabase — quest'ultima riconosciuta dai tre
allineamenti base64 con cui `service_role` può comparire nel payload del JWT.
La chiave `anon`, pubblica per progettazione, non viene toccata.

**Lo Stop hook non può creare un ciclo infinito.** Se blocca il turno e i test
continuano a fallire, al giro successivo `stop_hook_active` è `true` e l'hook
lascia terminare. La suite resta rossa — ma è una condizione visibile, non una
sessione bloccata.

**`mypy` gira sull'intero progetto, non sul singolo file.** Su un file isolato
non vedrebbe i tipi degli altri moduli e produrrebbe errori inesistenti. Del
risultato vengono però mostrate solo le righe del file appena modificato.

**Gli hook usano l'interprete di `backend/.venv`.** `ruff` e `mypy` di sistema
avrebbero versioni diverse da quelle pinnate in `requirements-dev.txt` e non
vedrebbero le dipendenze del progetto — `mypy` segnalerebbe come mancanti tutti
gli import di FastAPI e Pydantic.

**Errori inattesi non bloccano.** Payload illeggibile, `npx` assente,
`node_modules` non installato: l'hook esce 0. Un controllo che blocca il lavoro
per un problema proprio viene disattivato entro un'ora, e da quel momento non
protegge più niente.

## Verificare che gli hook siano attivi

La logica degli script è verificata; quello che dipende dalla macchina è se
Claude Code riesce a **invocarli** — in particolare se `$CLAUDE_PROJECT_DIR`
viene espanso dalla shell usata per gli hook, cosa che su Windows varia.

Prova diretta: chiedere a Claude di scrivere `frontend/prova-hook.ts` con
dentro `const k = process.env.GEMINI_API_KEY;`. La scrittura deve essere
**rifiutata**. Se invece va a buon fine, gli hook non stanno partendo.

Diagnosi, in ordine:

1. `claude --debug` mostra a ogni chiamata quali hook vengono eseguiti e con
   quale esito.
2. `/hooks` elenca la configurazione così come Claude Code l'ha effettivamente
   caricata.
3. Verifica manuale, che salta del tutto Claude Code:

   ```bash
   echo '{"tool_input":{"file_path":"frontend/x.ts","content":"process.env.GEMINI_API_KEY"}}' \
     | python .claude/hooks/block_frontend_secrets.py; echo "exit=$?"
   ```

   Deve stampare il messaggio di blocco e uscire con `2`. Se questo funziona ma
   l'hook non parte in sessione, il problema è nell'espansione del path: in
   `.claude/settings.json` si può sostituire `$CLAUDE_PROJECT_DIR` con il
   percorso assoluto del repository, ricordando che quel file è versionato e
   che un path assoluto vale solo su questa macchina (in tal caso conviene
   spostare la modifica in `.claude/settings.local.json`, che è gitignored).

## Costo

Lo Stop hook aggiunge il tempo della suite (~5 s) a ogni turno che ha toccato
`backend/`. `mypy` a cache fredda può superare il minuto, poi scende a pochi
secondi. Se diventasse fastidioso, la leva giusta è togliere `mypy` dal
`PostToolUse` — resta comunque bloccante in CI — non alzare i timeout.
