# Trading Command Center — one image with Freqtrade + TCC and the Hummingbot fork.
# Build context is the REPO ROOT:  docker compose -f command_center/docker-compose.yml build
#
# Layout inside the image (mirrors station.yaml's relative paths):
#   /app/freqtrade                  this repo (+ .venv)
#   /app/freqtrade/command_center   TCC
#   /app/army161/hummingbot         Hummingbot fork (+ .venv, Cython build)
FROM python:3.12-slim-bookworm

ARG HB_REPO=https://github.com/Army161/hummingbot
ARG HB_REF=master
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_LINK_MODE=copy UV_NATIVE_TLS=1 \
    PATH=/app/freqtrade/.venv/bin:/root/.local/bin:$PATH

# Optional extra CA certificates (only needed behind a TLS-intercepting proxy): drop *.crt
# files into command_center/docker/certs/ before building. Empty by default.
COPY command_center/docker/certs/ /usr/local/share/ca-certificates/extra/
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git curl ca-certificates tini \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# ---- Hummingbot (slow Cython build; own layer so code edits don't rebuild it) -------------
RUN git clone --depth 1 --branch "$HB_REF" "$HB_REPO" /app/army161/hummingbot \
    || git clone --depth 1 "$HB_REPO" /app/army161/hummingbot
WORKDIR /app/army161/hummingbot
RUN uv venv .venv --python /usr/local/bin/python3.12 \
    && uv pip install --python .venv/bin/python cython "numpy>=2.2.6" "setuptools==80.8.0" wheel \
    && uv pip install --python .venv/bin/python -e . --no-build-isolation \
    && uv pip install --python .venv/bin/python "ptpython>3.0.25" pyperclip objgraph pandas-ta \
    && .venv/bin/python -c "import hummingbot.strategy_v2.backtesting.backtesting_engine_base"

# ---- Freqtrade dependencies (cached unless pyproject/requirements change) ------------------
WORKDIR /app/freqtrade
COPY pyproject.toml requirements*.txt README.md ./
RUN uv venv .venv --python /usr/local/bin/python3.12 \
    && uv pip install --python .venv/bin/python -r requirements.txt

# ---- Freqtrade + TCC source ---------------------------------------------------------------
COPY . /app/freqtrade
RUN uv pip install --python .venv/bin/python -e . -e command_center \
    && mkdir -p user_data/data user_data/logs command_center/state \
    && python -c "import freqtrade, tcc, numba; print('ok')"

WORKDIR /app/freqtrade/command_center
ENV TCC_API_HOST=0.0.0.0 TCC_API_PORT=8800
EXPOSE 8800
HEALTHCHECK --interval=60s --timeout=10s --start-period=600s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8800/api/overview',timeout=8)"
ENTRYPOINT ["/usr/bin/tini", "--", "/app/freqtrade/command_center/docker/entrypoint.sh"]
CMD ["up"]
