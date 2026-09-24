FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEMORY_DROPBOX_ROOT=/var/lib/maestro/dropbox

WORKDIR /srv/maestro

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system maestro \
    && adduser --system --ingroup maestro --home /var/lib/maestro maestro \
    && mkdir -p /var/lib/maestro/dropbox \
    && chown -R maestro:maestro /var/lib/maestro

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic

USER maestro

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=3)"

CMD ["python", "-m", "app.operations.api"]
