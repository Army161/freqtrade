#!/usr/bin/env bash
# One-shot installer for the Trading Command Center (Linux/macOS).
#   - Freqtrade (this repo) into ../.venv  + the tcc package
#   - Hummingbot (your fork, Army161/hummingbot) into $HB_HOME with its own venv
#   - initial market data download
# Usage:  bash command_center/scripts/install.sh   [HB_HOME=~/army161/hummingbot]
set -euo pipefail

CC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FT_ROOT="$(cd "$CC_DIR/.." && pwd)"
HB_HOME="${HB_HOME:-$(cd "$FT_ROOT/.." && pwd)/army161/hummingbot}"
HB_REPO="${HB_REPO:-https://github.com/Army161/hummingbot}"

command -v uv >/dev/null || { echo "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }

echo "==> Freqtrade + TCC in $FT_ROOT/.venv"
cd "$FT_ROOT"
[ -d .venv ] || uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -e . -e "$CC_DIR" pytest

echo "==> Hummingbot in $HB_HOME"
if [ ! -d "$HB_HOME/.git" ]; then
  mkdir -p "$(dirname "$HB_HOME")"
  git clone --depth 1 "$HB_REPO" "$HB_HOME"
fi
cd "$HB_HOME"
[ -d .venv ] || uv venv .venv --python 3.12
uv pip install --python .venv/bin/python cython "numpy>=2.2.6" "setuptools==80.8.0" wheel
uv pip install --python .venv/bin/python -e . --no-build-isolation
# runtime deps that upstream installs via conda only
uv pip install --python .venv/bin/python "ptpython>3.0.25" pyperclip objgraph pandas-ta

echo "==> Market data"
cd "$CC_DIR"
"$FT_ROOT/.venv/bin/tcc" download

if [ "$HB_HOME" != "$(cd "$FT_ROOT/.." && pwd)/army161/hummingbot" ]; then
  echo "NOTE: set clusters.hummingbot.home: $HB_HOME in $CC_DIR/station.yaml"
fi
echo "==> Tests"
"$FT_ROOT/.venv/bin/python" -m pytest -q -c "$CC_DIR/pyproject.toml" --rootdir "$CC_DIR" "$CC_DIR/tests"
echo
echo "Done. Start the station:   cd $CC_DIR && ../.venv/bin/tcc up   ->  http://127.0.0.1:8800"
