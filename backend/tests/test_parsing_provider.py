"""`provider_parsing` non solleva mai: è il suo unico contratto forte.

Legge JSON che arriva da Apify e dalla YouTube Data API — **input non fidato**,
nel senso che nessuno ci garantisce la forma. Un parser che esplode su un campo
accessorio farebbe fallire un'analisi già pagata per un numero che finisce in una
card, quindi la regola è: davanti a un valore illeggibile si restituisce `None`,
mai un'eccezione e mai un default inventato.

Il file esiste perché unificare cinque copie ha rischiato di romperlo. La
revisione di sicurezza della PR ha trovato che la versione unificata sollevava
`OverflowError` su `Infinity` dove quella di YouTube restituiva `None`: una
regressione silenziosa in un refactoring dichiarato a comportamento invariato.
Correggendola sono emersi **due difetti preesistenti a `main`**, che ora sono
chiusi e qui bloccati:

* `json.loads` della stdlib accetta `Infinity` e `NaN`, che JSON non prevede, e
  `int(inf)` solleva `OverflowError` — capitava sul percorso Apify;
* `"²".isdigit()` è `True` ma `int("²")` solleva `ValueError` — capitava su
  entrambi i percorsi.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.provider_parsing import come_datetime, contatore, primo_contatore

# Valori che un provider non dovrebbe mandare e che prima o poi manda.
VALORI_OSTILI = [
    float("inf"),
    float("-inf"),
    float("nan"),
    "²",  # isdigit() è True, int() solleva
    "12e400",
    "",
    "   ",
    "-5",
    None,
    True,  # bool è sottotipo di int
    False,
    [],
    {},
    object(),
]


@pytest.mark.parametrize("valore", VALORI_OSTILI)
def test_un_contatore_illeggibile_da_none_e_non_solleva(valore: object) -> None:
    assert contatore(valore) is None


@pytest.mark.parametrize("valore", VALORI_OSTILI)
def test_anche_sondando_piu_chiavi_non_si_solleva(valore: object) -> None:
    assert primo_contatore({"likesCount": valore, "likes": valore}, ("likesCount", "likes")) is None


@pytest.mark.parametrize(
    ("grezzo", "atteso"),
    [
        (12300, 12300),
        ("12300", 12300),  # la Data API manda sempre stringhe
        ("1.234", 1234),  # alcuni actor formattano le migliaia
        ("1,234", 1234),
        (12300.0, 12300),
        (0, 0),  # zero è un conteggio vero, non un valore mancante
    ],
)
def test_i_conteggi_leggibili_passano(grezzo: object, atteso: int) -> None:
    assert contatore(grezzo) == atteso


def test_una_chiave_illeggibile_non_ferma_la_ricerca_sulle_altre() -> None:
    """«C'è ma non si capisce» e «non c'è» portano alla stessa domanda."""
    item = {"likesCount": float("inf"), "diggCount": "²", "likes": "42"}

    assert primo_contatore(item, ("likesCount", "diggCount", "likes")) == 42


@pytest.mark.parametrize("valore", ["", "non una data", "2026-13-45T00:00:00Z", 42, None, []])
def test_una_data_illeggibile_da_none_e_non_solleva(valore: object) -> None:
    assert come_datetime(valore) is None


def test_una_data_senza_fuso_viene_assunta_utc() -> None:
    """Un naive confrontato con un aware solleva TypeError: qui si chiude prima."""
    istante = come_datetime("2026-08-16T10:30:00")

    assert istante is not None
    assert istante.tzinfo is not None
    assert istante == datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


def test_la_z_di_rfc3339_viene_riconosciuta() -> None:
    """È la forma che manda la Data API, e `fromisoformat` non la accetta ovunque."""
    assert come_datetime("2026-08-16T10:30:00Z") == datetime(2026, 8, 16, 10, 30, tzinfo=UTC)
