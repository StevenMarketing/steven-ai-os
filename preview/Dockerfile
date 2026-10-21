# Private GReply preview image -- DEMO ONLY.
#
# Runs the offline fixture demo behind a login: loopback-bound, non-root, no
# live Google credentials, no publish path. Guarded by preview/guard.py.
#
# Build from the repository root:
#   docker build -f preview/Dockerfile -t greply-preview .
# Run (all three values are required, or the container refuses to start):
#   docker run --rm -p 127.0.0.1:8080:8080 \
#     -e GBP_PREVIEW=1 -e GBP_SESSION_SECRET=... -e GBP_DEMO_PASSWORD=... \
#     greply-preview
FROM python:3.12-slim

# No build toolchain and no pip installs: the app is standard-library only.
RUN useradd --create-home --uid 10001 preview
WORKDIR /app

COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY web/ /app/web/
COPY fixtures/ /app/fixtures/
COPY sql/ /app/sql/
COPY preview/ /app/preview/

RUN chmod 0755 /app/preview/entrypoint.sh

USER preview

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    GBP_PREVIEW=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/preview/entrypoint.sh"]
