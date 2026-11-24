#!/usr/bin/env bash
# Container entrypoint: first-run market data download, then the station.
set -euo pipefail
cd /app/freqtrade/command_center

if [ "${1:-up}" = "up" ]; then
  if ! python - <<'EOF'
import sys
from tcc.config import load_config
from tcc.data import has_data
sys.exit(0 if has_data(load_config()) else 1)
EOF
  then
    echo "[tcc] no market data yet -> downloading (first start takes a few minutes)"
    tcc download || echo "[tcc] WARNING: download failed; research will retry each cycle"
  fi
  if [ "${TCC_LIVE:-0}" = "1" ]; then
    echo "[tcc] TCC_LIVE=1 set: live trading only if station.yaml has mode: live AND keys are set"
  fi
  exec tcc up
fi
exec tcc "$@"
