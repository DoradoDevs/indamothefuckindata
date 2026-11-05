FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
COPY pipeline ./pipeline
RUN pip install --no-cache-dir .
ENV DATA_DIR=/data
VOLUME /data
# Default: serve the API. The scheduled job overrides the command with:
#   python -m pipeline.run --source hubspot
CMD ["uvicorn", "pipeline.publish.api:app", "--host", "0.0.0.0", "--port", "8000"]
