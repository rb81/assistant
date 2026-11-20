FROM python:3.12-slim-bookworm AS gateway
WORKDIR /app
COPY sandbox/egress_proxy.py web_reader/gateway.py ./
USER 10001:10001
CMD ["python", "-u", "gateway.py"]

FROM python:3.12-slim-bookworm AS reader
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
    PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir playwright==1.65.2 trafilatura==2.3.1
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY web_reader/service.py web_reader/extract.py ./
CMD ["python", "-u", "service.py"]
