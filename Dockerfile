# ai-council is pure Python standard library — no pip dependencies needed.
FROM python:3.12-slim

WORKDIR /app

COPY ai_council.py cli.py config.py council.py models.py ollama.py \
     prompts.py server.py store.py VERSION ./

ENV PYTHONUNBUFFERED=1 \
    AI_COUNCIL_DATA_DIR=/data

# Session history lives in /data; mount a volume on this path to persist it.
VOLUME ["/data"]

EXPOSE 6636

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6636/api/health', timeout=5)"

# Bind 0.0.0.0 so `docker run -p 6636:6636` works out of the box.
# docker-compose.yml overrides this to share the host network instead.
CMD ["python", "ai_council.py", "--server", "--host", "0.0.0.0", "--port", "6636", "--no-open"]
