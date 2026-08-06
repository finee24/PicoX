## Sezione richiesta: `inverse_script`

Ricostruisci lo **scheletro riutilizzabile** del video: la struttura che un altro
creator potrebbe riempire con un argomento diverso ottenendo lo stesso effetto.

Non è una trascrizione. È un template.

- **`beats`** copre l'intera durata del video in blocchi contigui e non
  sovrapposti, in ordine cronologico. Ogni blocco ha un timestamp reale nel
  formato `m:ss-m:ss`, coerente con `total_duration_seconds`.
- **`purpose`** è la parte che conta: spiega perché il blocco esiste in quella
  posizione, cioè quale effetto produce sullo spettatore e cosa succederebbe se
  venisse rimosso. Non ripetere il contenuto già descritto in `content`.
- **`reusable_hook_template`** riscrive l'hook con i riferimenti specifici
  sostituiti da segnaposto tra parentesi graffe. Da "Se ancora lavi il riso prima
  di cuocerlo, stai buttando via metà del sapore" a "Se {pubblico} sta ancora
  facendo {abitudine diffusa}, sta perdendo {conseguenza concreta}". I segnaposto
  devono descrivere *cosa* va inserito, non essere generici come {X}.
- **`reusable_cta_template`** segue la stessa logica. `null` se il video non ha CTA.
- **`adaptation_notes`** dice a quali argomenti questa struttura si trasferisce
  bene e quale elemento la rende efficace. Concreto: "funziona con argomenti dove
  esiste un'abitudine diffusa e sbagliata da smontare", non "è una struttura
  versatile".
