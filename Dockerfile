FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SMX_HOST=0.0.0.0 \
    SMX_PORT=8080 \
    SMX_DATABASE=/data/xerox.sqlite3 \
    SMX_STORAGE=/data/private-documents

WORKDIR /app
COPY --chown=10001:10001 . /app
RUN mkdir -p /data/private-documents \
    && chown -R 10001:10001 /app /data \
    && chmod 700 /data /data/private-documents

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
CMD ["python", "app.py"]
