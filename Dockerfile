# Dockerfile.litellm-custom
FROM ghcr.io/berriai/litellm:main-latest

WORKDIR /app

# 1️⃣  Drop in your fork (assumes it lives at ./litellm in your project)
COPY ./litellm /tmp/litellm
RUN pip install --no-cache-dir /tmp/litellm && rm -rf /tmp/litellm

# 2️⃣  Default entrypoint (same as the stock image)
ENTRYPOINT ["litellm", "--config", "/app/config.yaml", "--port", "4000", "--num_workers", "1"]
