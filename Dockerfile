FROM ghcr.io/astral-sh/uv:0.9.0-python3.12-trixie-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy \
    TZ=Asia/Shanghai PATH=/app/.venv/bin:$PATH
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini xvfb xauth x11-utils x11vnc novnc websockify \
    fluxbox fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev \
    && uv run --frozen python -m playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/*
COPY docker/smoke.py ./docker/smoke.py
RUN install -d -o app -g app -m 700 /app/.state \
    && install -d -o app -g app -m 755 /app/.venv/lib/python3.12/site-packages/fpgen/data
USER app
ARG CAMOUFOX_VERSION=""
ARG CAMOUFOX_DOWNLOAD_PROXY=""
RUN if [ -n "$CAMOUFOX_DOWNLOAD_PROXY" ]; then \
      export HTTP_PROXY="$CAMOUFOX_DOWNLOAD_PROXY" HTTPS_PROXY="$CAMOUFOX_DOWNLOAD_PROXY"; fi; \
    if [ -n "$CAMOUFOX_VERSION" ]; then python -m camoufox fetch "$CAMOUFOX_VERSION"; \
    else python -m camoufox fetch; fi \
    && python -c "from camoufox.pkgman import launch_path; print(launch_path())"
RUN if [ -n "$CAMOUFOX_DOWNLOAD_PROXY" ]; then \
      export HTTP_PROXY="$CAMOUFOX_DOWNLOAD_PROXY" HTTPS_PROXY="$CAMOUFOX_DOWNLOAD_PROXY"; fi; \
    python /app/docker/smoke.py
COPY --chown=app:app main.py README.md ./
COPY --chown=app:app auto_location/ ./auto_location/
COPY --chown=app:app docker/ ./docker/
USER root
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh
USER app
EXPOSE 8080 6080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/docker/entrypoint.sh"]
CMD ["service"]
