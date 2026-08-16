"""Lettura difensiva di ciò che restituiscono i provider esterni.

Apify e la YouTube Data API mandano JSON che **non controlliamo**: i tipi
cambiano fra versioni di un actor, i conteggi arrivano ora come numeri ora come
stringhe, e un campo assente non è distinguibile da un campo a zero se non
guardando la chiave. Da qui due funzioni sole, entrambe con la stessa regola:
**davanti a un valore che non si sa leggere si restituisce `None`, mai un
default inventato**. Un `0` al posto di "non lo so" è un dato falso che poi
nessuno riesce più a distinguere da una misura vera.

Perché stanno qui e non dentro uno dei due service: c'erano tre copie di
`fromisoformat(...replace("Z", "+00:00"))` e due parser di contatori, con lo
stesso concetto scritto una volta in inglese (`_parse_datetime`, `_first_int`)
e una in italiano (`_come_datetime`, `_contatore`). Metterle in
`apify_service` avrebbe fatto dipendere `youtube_service` da Apify, che è falso:
YouTube non passa da lì.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any


def come_datetime(value: Any) -> datetime | None:
    """Un istante da ciò che il provider ha mandato, sempre consapevole del fuso.

    Accetta un `datetime` già pronto (alcuni SDK lo parsano da sé) o una stringa
    ISO-8601. La `Z` finale di RFC 3339 va tradotta a mano: `fromisoformat` non
    la accetta prima di Python 3.11, e scriverlo esplicitamente rende il codice
    indipendente da quel dettaglio di versione.

    **Il fuso viene forzato a UTC quando manca.** Un datetime *naive* confrontato
    con uno *aware* solleva `TypeError`, e UTC è il fuso in cui il database
    scrive `now()`: assumerlo è l'unica ipotesi che non introduce uno sfasamento
    silenzioso. In pratica il ramo non si attiva quasi mai — Apify e YouTube
    mandano entrambi l'offset — ma copre la riga di cache scritta a mano.
    """
    if isinstance(value, datetime):
        istante = value
    elif isinstance(value, str) and value:
        try:
            istante = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    return istante if istante.tzinfo is not None else istante.replace(tzinfo=UTC)


def contatore(raw: Any) -> int | None:
    """Un conteggio (follower, like, visualizzazioni), o `None` se non leggibile.

    `None` e non `0`: su un video con i like nascosti la Data API **omette**
    `likeCount`, e su un profilo che nasconde i follower l'actor fa lo stesso.
    Zero sarebbe un'affermazione che non abbiamo.

    **Non solleva mai**, ed è la parte che conta: davanti a un valore
    illeggibile restituisce `None`. Un provider non è tenuto a rispettare le
    nostre aspettative, e un parser che esplode su un campo accessorio farebbe
    fallire un'analisi già pagata per un numero che finisce in una card.

    Le insidie, tutte incontrate davvero:

    * `bool` è sottotipo di `int` e passerebbe l'`isinstance`: un `True` letto
      come conteggio 1 sarebbe un dato inventato, non un dato mancante;
    * i conteggi arrivano spesso **come stringhe** — la Data API lo fa *sempre*
      (`subscriberCount: "12300"`);
    * alcuni actor li formattano con i separatori delle migliaia;
    * `json.loads` della stdlib **accetta `Infinity` e `NaN`**, che JSON non
      prevede, e `int(inf)` solleva `OverflowError`. Da qui `isfinite`;
    * `str.isdigit()` è vero anche per gli esponenti unicode (`"²"`), dove
      `int()` solleva `ValueError`. `isdecimal()` è l'insieme che `int()`
      accetta davvero.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and not math.isinf(raw) and not math.isnan(raw):
        return int(raw) if raw >= 0 else None
    if isinstance(raw, str):
        pulito = raw.replace(".", "").replace(",", "").strip()
        if pulito.isdecimal():
            return int(pulito)
    return None


def primo_contatore(item: dict[str, Any], chiavi: tuple[str, ...]) -> int | None:
    """Il primo conteggio leggibile fra più chiavi candidate.

    Gli actor cambiano nomenclatura fra versioni (`likesCount`, `likeCount`,
    `likes`): sondare più chiavi costa meno che inseguire ogni release. Una
    chiave presente ma illeggibile non ferma la ricerca — si prova la
    successiva, perché "c'è ma non si capisce" e "non c'è" portano entrambe alla
    stessa domanda: qualcun altro lo sa?
    """
    for chiave in chiavi:
        valore = contatore(item.get(chiave))
        if valore is not None:
            return valore
    return None
