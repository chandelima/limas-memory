FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git: the memory is a repository (Curator commits/pushes).
# system tzdata: without it the TZ variable is ignored and "today" becomes the UTC day.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    # the mounted folder may belong to another host user: git would refuse the repo without this
    && git config --system --add safe.directory '*'

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src

# Data outside the image: /memory (the memory git repository) and /data (conversation history and briefing state).
ENV MEMORY_DIR=/memory \
    CONVERSATION_FILE=/data/conversation.json \
    BRIEFING_STATE_FILE=/data/briefing.json

RUN useradd --create-home --uid 1000 limas && mkdir -p /data /memory && chown limas /data /memory
USER limas
VOLUME ["/data", "/memory"]

CMD ["python", "src/bot.py"]
