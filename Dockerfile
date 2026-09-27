FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BRAILLE_DATA_DIR=/data \
    BRAILLE_HOST=0.0.0.0 \
    BRAILLE_PORT=8080

WORKDIR /srv
COPY app/ /srv/app/

# Non-root runtime user; /data is provided as a volume.
RUN useradd --create-home --uid 10001 braille \
    && mkdir -p /data \
    && chown -R braille:braille /data /srv
USER braille

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "app.main"]
