#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "This release recipe builds Linux x86-64 only." >&2
  exit 2
fi

python -m pip install -e '.[release]'
python -m PyInstaller --clean --noconfirm --onedir --name mm-engine \
  --collect-submodules kdf_mm --exclude-module PySide6 \
  scripts/mm_engine_entrypoint.py

tag="${1:-local}"
archive="dist/mm-engine-${tag}-linux-x86_64.tar.gz"
tar -C dist -czf "$archive" mm-engine
sha256sum "$archive"
echo "Local candidate only: sign a compatibility manifest before publishing."
