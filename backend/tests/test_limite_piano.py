"""Il limite di piano deve arrivare al client come tale, non come guasto.

Il tetto ai creator attivi è imposto da un trigger (migration 0004), che
solleva lo SQLSTATE `PX001`. Senza una traduzione dedicata quell'errore
cadrebbe nel ramo generico di `translate_postgrest_error` e l'utente
riceverebbe un `503 database_unavailable` — mandandolo a cercare un guasto
inesistente mentre ha semplicemente raggiunto il limite del proprio piano.

Il trigger vive nel database e i test girano su un doppio in memoria, quindi
qui si verifica il confine che il doppio *può* riprodurre fedelmente: che
l'errore sollevato da Postgres diventi l'eccezione di dominio giusta, con un
messaggio che non porta con sé dettagli interni.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ConflictError, DatabaseError, PlanLimitError
from app.services.supabase_service import translate_postgrest_error
from tests.fake_supabase import FakeAPIError

MESSAGGIO_POSTGRES = "Limite di 30 creator attivi raggiunto per il piano free."


def test_lo_sqlstate_del_tetto_diventa_un_errore_di_piano() -> None:
    errore = translate_postgrest_error(
        FakeAPIError("PX001", MESSAGGIO_POSTGRES), context="insert creator"
    )

    assert isinstance(errore, PlanLimitError)
    assert errore.status_code == 409
    assert errore.code == "plan_limit_reached"


def test_il_messaggio_al_client_non_contiene_dettagli_del_database() -> None:
    """Il testo di Postgres resta nei log, come per ogni altro errore tradotto."""
    errore = translate_postgrest_error(
        FakeAPIError("PX001", MESSAGGIO_POSTGRES), context="insert creator"
    )

    testo = str(errore).lower()
    for frammento in ("px001", "sqlstate", "plpgsql", "enforce_creator_limit",
                      "public.creators", "trigger"):
        assert frammento not in testo
    # E dice comunque cosa fare.
    assert "piano" in testo


@pytest.mark.parametrize(
    ("codice", "atteso"),
    [
        ("23505", ConflictError),
        ("23503", ConflictError),
        ("PX001", PlanLimitError),
        # Un codice sconosciuto deve restare generico: è la difesa contro il
        # leak, e non va allentata per far posto ai casi nuovi.
        ("42883", DatabaseError),
    ],
)
def test_ogni_sqlstate_finisce_nella_sua_eccezione(codice: str, atteso: type) -> None:
    errore = translate_postgrest_error(
        FakeAPIError(codice, "dettaglio interno del database"), context="prova"
    )
    assert isinstance(errore, atteso)
    assert "dettaglio interno" not in str(errore)
