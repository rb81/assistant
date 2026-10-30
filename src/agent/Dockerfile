FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg pandoc weasyprint \
    && rm -rf /var/lib/apt/lists/*

COPY src/agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/agent/src ./src
COPY docs ./docs

CMD ["python", "-m", "assistant_agent.main"]
