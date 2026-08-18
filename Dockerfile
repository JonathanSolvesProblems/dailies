# Slim rather than alpine: the MCP and gRPC wheels are manylinux, and alpine would
# force a source build of things that already ship binaries.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg is deliberately NOT installed. The hosted surface only reads scene state that
# the pipeline already produced; frame sampling happens where the footage lives, which
# is never on this container. Leaving it out keeps the image small and the attack
# surface smaller.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline/ ./pipeline/
COPY web/ ./web/
COPY samples/ ./samples/

# The shipped dataset. Real scene state from real takes, with the frame paths stripped
# because the frames themselves are private footage and are not distributed.
ENV DAILIES_OUT=/app/samples

# Cloud Run injects PORT and it is not always 8080. Honour it rather than hardcoding.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn web.app:app --host 0.0.0.0 --port ${PORT}
