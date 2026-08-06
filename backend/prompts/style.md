## Sezione richiesta: `style_analysis`

Analizza **come** il video è costruito: la forma, non l'argomento.

Guarda il video almeno due volte: la prima per l'impressione d'insieme, la
seconda contando quello che va contato.

- **Hook.** Trascrivi letteralmente le parole dei primi 3 secondi, nella lingua
  originale. Se in quei secondi non si parla, trascrivi il testo a schermo; se non
  c'è nemmeno quello, descrivi in una frase l'immagine di apertura.
- **Ritmo.** `average_shot_duration_seconds` va stimato contando i tagli visibili
  e dividendo la durata totale per il loro numero, non "a sensazione". Il valore di
  `pacing` deve essere coerente con quel numero.
- **Montaggio.** Elenca solo tecniche che puoi indicare in un punto preciso del
  video. Se vedi un solo jump cut, non scrivere "uso sistematico del jump cut".
- **Testo a schermo.** Distingui i sottotitoli integrali dalle parole chiave
  enfatizzate: sono due scelte editoriali diverse. Se non c'è testo, dillo.
- **Audio.** La musica ha un ruolo (scandisce i tagli, riempie il silenzio, cita un
  trend) oppure è tappezzeria: dove è riconoscibile, dillo.
- **CTA.** Solo se esiste un invito esplicito all'azione. "Seguimi per la parte 2"
  è una CTA; un finale che semplicemente si interrompe non lo è. In quel caso
  `cta` deve essere `null`.
