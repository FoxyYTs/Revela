#!/usr/bin/env bash
# Construye la capa de Lambda con Pillow para python3.12 x86_64 (la llama Terraform con data "external").
# Solo la reconstruye si no existe, para que el hash no cambie en cada plan. Stdout debe ser solo el JSON.
set -euo pipefail
dir="$(cd "$(dirname "$0")/.." && pwd)/infra/build"
zip="$dir/pillow-layer.zip"
if [[ ! -f "$zip" ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  uv pip install -q --target "$tmp/python" --python-version 3.12 --python-platform x86_64-manylinux2014 \
    --only-binary :all: "pillow==11.3.0" >&2
  mkdir -p "$dir"
  (cd "$tmp" && zip -qr "$zip" python) >&2
fi
printf '{"path":"%s"}\n' "$zip"
