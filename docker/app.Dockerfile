# One image for the API, the workers, the sandbox manager and the fake servers.
# The compose file picks the command. The sandbox image is separate (sandbox.Dockerfile).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# /data and /models exist in the image, owned by the app user, so that a new named volume
# mounted there starts out writable by it (Docker copies the ownership into an empty volume).
RUN pip install . \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin lnd \
 && mkdir /data /models && chown 10001:10001 /data /models

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "lnd.api:app", "--host", "0.0.0.0", "--port", "8000"]
