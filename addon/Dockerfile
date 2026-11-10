ARG BUILD_FROM=python:3.11-slim-bookworm
FROM ${BUILD_FROM}
ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm ca-certificates tini && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'aider-chat==0.86.1'
RUN useradd --create-home --uid 10001 coder && mkdir -p /data/workspace && chown -R coder:coder /data/workspace
COPY run.sh /run.sh
COPY job_runner.py submit_job.py checkout_repo.py preflight.py launcher.py publisher.py gemini_probe.py direct_editor.py refresh_repo.py queue_worker.py /opt/worker/
RUN chmod 755 /run.sh /opt/worker/job_runner.py /opt/worker/submit_job.py /opt/worker/checkout_repo.py /opt/worker/preflight.py /opt/worker/launcher.py /opt/worker/publisher.py /opt/worker/gemini_probe.py /opt/worker/direct_editor.py /opt/worker/refresh_repo.py /opt/worker/queue_worker.py
WORKDIR /data/workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/run.sh"]
