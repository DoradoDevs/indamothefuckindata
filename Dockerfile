FROM python:3.13-slim

# Claude Code CLI (Node 18+) for the analysis passes
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pipeline ./pipeline
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app /data
USER app

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

# Default: serve the API + dashboard. The scheduled job overrides the command with:
#   python -m pipeline.run --source hubspot
# and a weekly cleanup with:
#   python -m pipeline.prune
CMD ["uvicorn", "pipeline.publish.api:app", "--host", "0.0.0.0", "--port", "8000"]
