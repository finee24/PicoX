"""Il filtro sul proprietario è applicato dal builder, non da chi lo usa.

`ScopedTable` esiste perché la regola «ogni query filtra su `user_id`» non
dipenda dalla memoria di chi scrive una route. Fino a questo file però la regola
non era verificata da nessuna parte: rimuovendo il `.eq(...)` da
`ScopedTable.select` l'intera suite restava **verde**.

Il buco non era teorico. Col client service-role il RLS è scavalcato per
costruzione, quindi una `select` senza filtro restituisce le righe di *tutti* i
tenant — ed è esattamente la fuga che la classe esiste per rendere impossibile.
Nessun test la esercitava perché tutti i percorsi che leggono per conto di un
utente passano dal client scoped, dove il RLS del doppio maschera l'errore.

Qui si interroga il builder **direttamente**, col client che non ha rete sotto.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.services.supabase_service import service_table
from tests.conftest import USER_ID
from tests.fake_supabase import FakeStore

# Un secondo tenant, che nei risultati non deve comparire mai.
ALTRO_UTENTE = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def due_tenant(store: FakeStore) -> FakeStore:
    """Un insight a testa, per due utenti diversi."""
    store.seed("insights", {"user_id": USER_ID, "video_url": "https://x/mio"})
    store.seed("insights", {"user_id": ALTRO_UTENTE, "video_url": "https://x/altrui"})
    return store


async def test_una_select_service_role_vede_solo_le_righe_del_proprietario(
    due_tenant: FakeStore,
) -> None:
    """È il test che mancava: senza il filtro, qui uscirebbero due righe."""
    insights = await service_table("insights", USER_ID)

    risultato = await insights.select("*").execute()

    righe = cast(list[dict[str, Any]], risultato.data)
    assert [riga["video_url"] for riga in righe] == ["https://x/mio"]


async def test_una_delete_service_role_non_tocca_le_righe_altrui(
    due_tenant: FakeStore,
) -> None:
    insights = await service_table("insights", USER_ID)

    await insights.delete().execute()

    rimaste = [riga["user_id"] for riga in due_tenant.rows("insights")]
    assert rimaste == [ALTRO_UTENTE]


async def test_un_update_service_role_non_tocca_le_righe_altrui(
    due_tenant: FakeStore,
) -> None:
    insights = await service_table("insights", USER_ID)

    await insights.update({"video_url": "https://x/aggiornato"}).execute()

    per_utente = {riga["user_id"]: riga["video_url"] for riga in due_tenant.rows("insights")}
    assert per_utente[USER_ID] == "https://x/aggiornato"
    assert per_utente[ALTRO_UTENTE] == "https://x/altrui"


async def test_un_update_non_puo_riassegnare_la_riga_a_un_altro_utente(
    due_tenant: FakeStore,
) -> None:
    """Il proprietario è scartato dal payload, non applicato e poi filtrato."""
    insights = await service_table("insights", USER_ID)

    await insights.update(
        {"user_id": ALTRO_UTENTE, "video_url": "https://x/mio2"}
    ).execute()

    mie = [r for r in due_tenant.rows("insights") if r["user_id"] == USER_ID]
    assert len(mie) == 1
    assert mie[0]["video_url"] == "https://x/mio2"


async def test_un_insert_ignora_il_proprietario_passato_nel_payload(
    store: FakeStore,
) -> None:
    """Un `user_id` nel payload non deve poter dirottare la riga."""
    insights = await service_table("insights", USER_ID)

    await insights.insert(
        {"user_id": ALTRO_UTENTE, "video_url": "https://x/tentativo"}
    ).execute()

    assert [riga["user_id"] for riga in store.rows("insights")] == [USER_ID]


async def test_un_upsert_senza_proprietario_nel_conflitto_e_rifiutato() -> None:
    """Un `on_conflict` senza `user_id` collide con la riga di un altro tenant.

    Il service-role può sovrascriverla, quindi il rifiuto deve arrivare **prima**
    della query, non dal database.
    """
    insights = await service_table("insights", USER_ID)

    with pytest.raises(RuntimeError, match="user_id"):
        insights.upsert({"video_url": "https://x/y"}, on_conflict="video_url")


async def test_senza_user_id_la_tabella_scopata_si_rifiuta_di_esistere() -> None:
    """Meglio un errore rumoroso di una query che filtra su stringa vuota."""
    with pytest.raises(RuntimeError, match="senza user_id"):
        await service_table("insights", "")
