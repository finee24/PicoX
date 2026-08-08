"""Il limite di durata non deve poter collassare in silenzio.

Storia del bug che questi test proteggono: `probe_duration_seconds` restituisce
`None` quando `ffprobe` non c'è, e `_enforce_duration(None)` era un no-op. Sul
runtime Python di Render — che non include ffmpeg — il risultato era che
`MAX_VIDEO_DURATION_SECONDS` era impostata, documentata e completamente inerte.
Nessun log, nessun errore: la configurazione dichiarava un limite che il runtime
non applicava.

Il deploy è passato a `runtime: docker`, che ffmpeg ce l'ha. Questi test servono
perché la prossima volta che ffprobe non c'è — altro provider, altro runtime, un
probe che fallisce su un file corrotto — il limite fallisca **chiuso** e in modo
rumoroso, invece di sparire di nuovo.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.exceptions import PicoxValidationError, VideoTooLongError
from app.services.media_service import _enforce_duration

# =============================================================================
# Dopo il download: una durata ignota è un rifiuto
# =============================================================================


def test_durata_ignota_dopo_il_download_rifiuta_il_video() -> None:
    """Il caso preciso: `ffprobe` assente -> probe `None` -> NON un no-op."""
    with pytest.raises(PicoxValidationError) as errore:
        _enforce_duration(None, get_settings(), verificabile=False)

    # Il messaggio deve dire *perché*, altrimenti chi lo legge in produzione
    # pensa che il video sia troppo lungo.
    assert "durata" in str(errore.value).lower()


def test_durata_nota_e_sotto_il_limite_passa_anche_in_fail_closed() -> None:
    settings = get_settings()
    _enforce_duration(
        settings.max_video_duration_seconds - 1, settings, verificabile=False
    )


def test_durata_nota_e_sopra_il_limite_viene_rifiutata() -> None:
    settings = get_settings()
    with pytest.raises(VideoTooLongError):
        _enforce_duration(
            settings.max_video_duration_seconds + 1, settings, verificabile=False
        )


# =============================================================================
# Prima del download: l'incertezza resta ammessa
# =============================================================================


def test_il_precontrollo_su_metadati_apify_assenti_non_rifiuta() -> None:
    """`known_duration_seconds` è best effort e quasi sempre `None`.

    Rendere fail-closed anche questo controllo rifiuterebbe ogni video che Apify
    non è riuscito a descrivere, prima ancora di scaricarlo — cioè quasi tutti.
    È il motivo per cui il fail-closed è mirato al solo controllo post-download.
    """
    _enforce_duration(None, get_settings())


def test_il_precontrollo_rifiuta_comunque_una_durata_nota_eccessiva() -> None:
    settings = get_settings()
    with pytest.raises(VideoTooLongError):
        _enforce_duration(settings.max_video_duration_seconds + 1, settings)
