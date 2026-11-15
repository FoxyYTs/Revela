#!/usr/bin/env bash
# Borra todas las colecciones de caras de Revela (datos biométricos). Las crea la API cuando alguien activa
# "Personas", así que terraform destroy no las ve: correr esto después de la exposición, antes o después del destroy.
set -euo pipefail
PREFIJO="${1:-jyj-revela-caras-}"
mapfile -t colecciones < <(aws rekognition list-collections --query "CollectionIds[?starts_with(@, '$PREFIJO')]" --output text | tr '\t' '\n' | grep -v '^$' || true)
if [ ${#colecciones[@]} -eq 0 ]; then echo "No hay colecciones $PREFIJO*"; exit 0; fi
printf '%s\n' "${colecciones[@]}"
read -rp "¿Borrar estas ${#colecciones[@]} colecciones? [s/N] " ok
[ "$ok" = "s" ] || exit 1
for c in "${colecciones[@]}"; do aws rekognition delete-collection --collection-id "$c" >/dev/null && echo "borrada $c"; done
