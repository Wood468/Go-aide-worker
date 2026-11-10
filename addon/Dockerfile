ARG BUILD_FROM=python:3.11-slim-bookworm
FROM ${BUILD_FROM}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm ca-certificates tini && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'aider-chat==0.86.1'
RUN useradd --create-home --uid 10001 coder && mkdir -p /data/workspace && chown -R coder:coder /data/workspace
COPY run.sh /run.sh
COPY job_runner.py submit_job.py /opt/worker/
RUN chmod 755 /run.sh /opt/worker/job_runner.py /opt/worker/submit_job.py
USER coder
WORKDIR /data/workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/run.sh"]
