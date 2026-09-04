FROM ghcr.io/astral-sh/uv:0.12.7-python3.12-trixie-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends gosu tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY agentscroll ./agentscroll

RUN uv pip install --system --no-cache . \
    && mkdir -p /app/outputs/hotlists /app/outputs/knowledge /app/outputs/shares

COPY docker/entrypoint.sh /usr/local/bin/agentscroll-entrypoint
RUN chmod 0755 /usr/local/bin/agentscroll-entrypoint

ENTRYPOINT ["agentscroll-entrypoint"]
CMD ["--help"]
