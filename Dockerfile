# Minimal Dockerfile to run the bazosbot monitor
FROM python:3.11-slim

# avoid buffering for logs
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# system deps (if needed) and install Python deps
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# copy app
COPY . /app

# non-root user
RUN useradd -m botuser || true

# data dir needs to be writable (create and chown while still root)
RUN mkdir -p /app/data && chown botuser:botuser /app/data

# switch to non-root user
USER botuser

WORKDIR /app

ENTRYPOINT ["python", "-m", "src.bazosbot.main"]
