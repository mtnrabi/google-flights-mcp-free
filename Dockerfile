# Always-on HTTP service, same shape as backend/Dockerfile (which deploys to
# Render as flymyg-api-latest). This is NOT a Lambda -- the MCP server holds
# long-lived streamable-HTTP connections and calls the API Lambda outbound.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Call logs are written here. Mount a volume to keep them across deploys --
# the rolling backend-spend guard rebuilds its 24h window from this file on
# startup, so losing it hands the process a fresh budget.
RUN mkdir -p /app/logs
ENV LOG_PATH=/app/logs/mcp_calls.jsonl

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "src"]
