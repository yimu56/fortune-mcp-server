# Fortune 记账 MCP Server（零第三方依赖，镜像很小）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=18088 \
    MCP_PATH=/mcp \
    FORTUNE_BASE_URL=http://fortune.example.com:46031/prod-api \
    FORTUNE_TOKEN_CACHE_DIR=/data

WORKDIR /app
COPY server.py fortune_api.py fortune_tools.py smoke_test.py healthcheck.py ./

RUN useradd -r -u 10001 -s /sbin/nologin fortune \
    && mkdir -p /data && chown -R fortune:fortune /data /app
USER fortune

EXPOSE 18088
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python /app/healthcheck.py

CMD ["python", "server.py"]
