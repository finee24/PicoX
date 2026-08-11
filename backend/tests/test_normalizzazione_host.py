"""La porta non deve rompere la chiave di cache (voce A8 dell'audit).

`normalize_video_url` costruisce la chiave con cui `insights` deduplica: due
forme dello stesso URL che producono due chiavi diverse sono due righe e **due
inferenze pagate**, per un video solo. Il difetto stava nell'uso di
`parts.netloc`, che include la porta, al posto di `parts.hostname`.

PERIMETRO. Qui si verifica **solo** la parte host della normalizzazione. La
canonicalizzazione per piattaforma — `youtu.be` contro `/shorts/`, le tre forme
di path di Instagram, il punto finale dell'FQDN, il percent-encoding, i redirect
brevi — e' la voce A9, ancora aperta e legata a una decisione non presa: quanto
normalizzare senza rischiare di **fondere video distinti**. Aggiungere qui test
su quei casi fisserebbe un comportamento che A9 dovra' poter cambiare.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import PicoxValidationError
from app.services.media_service import normalize_video_url

VIDEO = "/@creator/video/7522889737288305942"


def test_la_porta_esplicita_non_cambia_la_chiave() -> None:
    """Il caso che ha motivato il fix: `:443` e' invisibile all'utente.

    Prima della correzione queste due forme producevano chiavi diverse, quindi
    lo stesso video veniva analizzato e pagato due volte.
    """
    senza = normalize_video_url(f"https://tiktok.com{VIDEO}")
    con = normalize_video_url(f"https://tiktok.com:443{VIDEO}")

    assert con == senza
    assert ":443" not in con


@pytest.mark.parametrize(
    "url",
    [
        "https://tiktok.com:443" + VIDEO,
        "http://tiktok.com:80" + VIDEO,
        "https://tiktok.com:8443" + VIDEO,
        "https://TikTok.com:443" + VIDEO,
        "https://www.tiktok.com:443" + VIDEO,
    ],
)
def test_ogni_forma_con_porta_collassa_sulla_stessa_chiave(url: str) -> None:
    """Anche una porta non standard: lo schema canonico e' comunque `https`.

    `http://x:8080` e `https://x` collassavano gia' prima sullo schema forzato,
    quindi perdere la porta non fonde risorse che altrimenti resterebbero
    distinte.
    """
    assert normalize_video_url(url) == normalize_video_url(f"https://tiktok.com{VIDEO}")


def test_le_credenziali_nell_url_non_finiscono_nella_chiave() -> None:
    """`hostname` scarta da se' la userinfo, che prima si toglieva a mano.

    Una credenziale nella chiave di cache la renderebbe anche un posto in cui
    un segreto viene scritto in chiaro su una riga di `insights`.
    """
    chiave = normalize_video_url(f"https://utente:parolachiave@tiktok.com{VIDEO}")

    assert chiave == normalize_video_url(f"https://tiktok.com{VIDEO}")
    assert "utente" not in chiave
    assert "parolachiave" not in chiave


def test_la_porta_non_confonde_gli_alias_di_host() -> None:
    """Gli alias si applicano all'host, che ora arriva gia' senza porta.

    Con `netloc`, `m.tiktok.com:443` non combaciava con nessuna chiave di
    `_HOST_ALIASES` e restava un host a se': il fix chiude anche questo.
    """
    atteso = normalize_video_url(f"https://tiktok.com{VIDEO}")

    assert normalize_video_url(f"https://m.tiktok.com:443{VIDEO}") == atteso
    assert normalize_video_url(f"https://www.tiktok.com:443{VIDEO}") == atteso


@pytest.mark.parametrize("url", ["https://", "https:///percorso", "non-un-url", "   "])
def test_un_url_senza_host_resta_rifiutato(url: str) -> None:
    """Il controllo di validita' non si e' perso passando a `hostname`."""
    with pytest.raises(PicoxValidationError):
        normalize_video_url(url)
