FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dnsutils \
        file \
        git \
        iputils-ping \
        jq \
        unzip \
        wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py bootstrap.py egress_proxy.py .

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--ssl-certfile", "/run/assistant/runtime/current/sandbox.crt", "--ssl-keyfile", "/run/assistant/runtime/current/sandbox.key"]
