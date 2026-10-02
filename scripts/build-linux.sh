#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "This release recipe builds Linux x86-64 only." >&2
  exit 2
fi

python -m pip install -e '.[release]'
python -m PyInstaller --clean --noconfirm --onefile \
  --name mm-engine-linux-x86_64 \
  --collect-submodules kdf_mm --exclude-module PySide6 \
  scripts/mm_engine_entrypoint.py

sha256sum dist/mm-engine-linux-x86_64
echo "Local candidate only: publish only as a GitHub immutable release."
