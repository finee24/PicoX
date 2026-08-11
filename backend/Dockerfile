# =============================================================================
# Picox — backend API
# Immagine pronta per Render / Railway (entrambe passano la porta via $PORT).
# =============================================================================

# La suite Debian è pinnata di proposito. `python:3.11-slim` senza suffisso è un
# alias mobile: puntava a bookworm e oggi punta a trixie, quindi lo stesso
# Dockerfile costruito a distanza di mesi produceva un sistema operativo diverso
# — e con esso un `ffmpeg` diverso, da 5.1 a 7.1.
#
# Per un'immagine la cui unica ragione di esistere è «serve `ffmpeg`», un cambio
# di major del binario che la giustifica deve essere una decisione, non un
# effetto collaterale di quando si è premuto build.
#
# Pinnata a **trixie** e non a bookworm: l'immagine non è mai stata costruita —
# la verifica end-to-end di `docker compose` è la voce A13, ancora aperta —
# quindi non esiste una versione "già funzionante" da preservare. Trixie è ciò
# che l'alias risolve oggi, quindi questo pin **non cambia nulla** di ciò che un
# build produce: lo rende soltanto esplicito. Tornare a bookworm sarebbe stato
# un downgrade reale travestito da stabilizzazione.
FROM python:3.11-slim-trixie AS base

# - PYTHONDONTWRITEBYTECODE: niente .pyc in un filesystem effimero
# - PYTHONUNBUFFERED: i log arrivano al collector senza attendere il flush
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg fornisce `ffprobe`, con cui si verifica la durata del video prima di
# pagarne l'inferenza. Senza, resta attivo il solo limite di dimensione.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Le dipendenze prima del codice: il layer resta in cache finché
# requirements.txt non cambia.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Utente non privilegiato: un'esecuzione come root darebbe a un'eventuale RCE
# il controllo pieno del container.
RUN useradd --create-home --uid 1000 picox \
    && chown -R picox:picox /app
USER picox

ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4).status == 200 else 1)"

# `sh -c` per l'espansione di $PORT: Render e Railway assegnano la porta a runtime.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
