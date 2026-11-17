FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git: a memória é um repositório (commit/push do Curator).
# tzdata do sistema: sem ele a variável TZ é ignorada e "hoje" vira o dia em UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    # a pasta montada pode pertencer a outro usuário do host: o git recusaria o repo sem isso
    && git config --system --add safe.directory '*'

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src

# Dados fora da imagem: /memory (repositório git da memória) e /data (histórico da conversa e estado do resumo).
ENV MEMORY_DIR=/memory \
    CONVERSATION_FILE=/data/conversa.json \
    BRIEFING_STATE_FILE=/data/briefing.json

RUN useradd --create-home --uid 1000 limas && mkdir -p /data /memory && chown limas /data /memory
USER limas
VOLUME ["/data", "/memory"]

CMD ["python", "src/bot.py"]
