FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/tracefix PATH=/opt/tracefix/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /opt/requirements.lock
RUN pip install --no-cache-dir -r /opt/requirements.lock
ARG BUILD_MANIFEST
LABEL org.tracefix.eval.build-manifest=$BUILD_MANIFEST
COPY tracefix /opt/tracefix/tracefix
COPY evals /opt/tracefix/evals
RUN mkdir -p /opt/tracefix/bin && cp /opt/tracefix/evals/docker_proxy.py /opt/tracefix/bin/docker && chmod 755 /opt/tracefix/bin/docker && useradd -m -u 1000 agent
USER 1000:1000
WORKDIR /agent-run
CMD ["python", "-m", "evals.docker_worker", "serve"]
