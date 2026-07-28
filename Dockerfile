# syntax=docker/dockerfile:1

# Alpine is safe here: the only dependency is prometheus-client, which is pure
# Python, so musl never has to satisfy a compiled extension. Saves ~90 MB.
FROM python:3.12-alpine AS build
WORKDIR /src
COPY pyproject.toml README.md pa2_exporter.py pa2_poc.py ./
# Self-contained venv, copied wholesale into the runtime stage so pip, wheels
# and build metadata never reach the published image.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-alpine
# image.source is injected by the release workflow (docker/metadata-action),
# so it stays correct for whatever repo this is published from.
LABEL org.opencontainers.image.title="pa2_exporter" \
      org.opencontainers.image.description="Prometheus exporter for the dbx DriveRack PA2" \
      org.opencontainers.image.licenses="MIT"

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN adduser -D -H -u 10001 pa2
USER pa2

EXPOSE 10048

# Liveness of the exporter only: a powered-off PA2 is not an unhealthy
# container, it is a fact the exporter reports as pa2_up 0. Do not check that
# here or a dark venue will send the orchestrator into a restart loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", \
         "import urllib.request,os,sys; \
port=os.environ.get('PA2_EXPORTER_PORT','10048'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["pa2-exporter"]
