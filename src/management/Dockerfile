FROM docker:29-cli AS docker-cli
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY src/agent/requirements-setup.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY src/agent/src /app/src
COPY src/config/agent.yaml src/config/agent.schema.yaml /app/defaults/
RUN chmod -R a+rX /app/src /app/defaults
ARG ASSISTANT_REVISION=unknown
ENV ASSISTANT_REVISION=$ASSISTANT_REVISION
CMD ["python", "-m", "assistant_agent.management_server", "serve"]
