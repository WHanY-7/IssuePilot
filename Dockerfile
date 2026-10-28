FROM python:3.12-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid "${APP_GID}" issuepilot \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home issuepilot

COPY requirements.docker.lock /app/requirements.docker.lock
RUN python -m pip install --no-cache-dir --requirement /app/requirements.docker.lock

COPY issuepilot /app/issuepilot
COPY data/issues_demo.json /app/seed/issues_demo.json
COPY docker/healthcheck.py /app/docker/healthcheck.py
COPY docker/mock_worker.py /app/docker/mock_worker.py

RUN mkdir -p /var/lib/issuepilot \
    && chown -R issuepilot:issuepilot /var/lib/issuepilot /home/issuepilot

USER issuepilot

EXPOSE 8790
STOPSIGNAL SIGTERM

CMD ["python", "-m", "uvicorn", "issuepilot.api:app", "--host", "0.0.0.0", "--port", "8790"]
