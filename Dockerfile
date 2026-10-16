FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Ho_Chi_Minh \
    CHATGPT2API_BUILD_TYPE=release \
    CHATGPT2API_HOST=0.0.0.0 \
    CHATGPT2API_PORT=3000

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    gcc \
    openssl \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py VERSION studio_upstream_errors.py ./
COPY api ./api
COPY contracts ./contracts
COPY services ./services
COPY utils ./utils
COPY web_dist ./web_dist
COPY assets ./assets

RUN mkdir -p /app/data

EXPOSE 3000

VOLUME ["/app/data"]

CMD ["python", "main.py"]
