FROM debian:bookworm-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git cmake build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git init whisper && cd whisper && git remote add origin https://github.com/ggml-org/whisper.cpp.git && \
    git fetch --depth 1 origin a8d002cfd879315632a579e73f0148d06959de36 && git checkout --detach FETCH_HEAD && \
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DWHISPER_BUILD_TESTS=OFF && \
    cmake --build build --target whisper-cli --parallel 2

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 && rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 transcription && useradd --uid 10001 --gid 10001 --no-create-home transcription && \
    mkdir /models && chown 10001:10001 /models
RUN pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn>=0.34,<1'
COPY --from=builder /build/whisper/build/bin/whisper-cli /usr/local/bin/whisper-cli
WORKDIR /app
COPY server.py bootstrap.py ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001:10001
ENTRYPOINT ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-keep-alive", "5", "--limit-concurrency", "4"]