#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-}"
ENV_FILE="${2:-/opt/hermes-stock/current/backend/.env}"
BACKUP_DIR="${3:-/opt/hermes-stock/shared/env-backups}"

if [[ "$MODE" != "milvus" && "$MODE" != "chroma" ]]; then
  echo "usage: $0 {milvus|chroma} [env-file] [backup-dir]" >&2
  exit 2
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "environment file does not exist: $ENV_FILE" >&2
  exit 2
fi

umask 077
install -d -m 0700 "$BACKUP_DIR"
backup="$BACKUP_DIR/.env.$(date +%Y%m%d%H%M%S).pre-$MODE"
cp -p "$ENV_FILE" "$backup"

set_key() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    index($0, key "=") == 1 {
      if (!found) print key "=" value
      found = 1
      next
    }
    { print }
    END { if (!found) print key "=" value }
  ' "$ENV_FILE" > "$tmp"
  chmod --reference="$ENV_FILE" "$tmp"
  chown --reference="$ENV_FILE" "$tmp"
  mv -f "$tmp" "$ENV_FILE"
}

set_key STOCK_VECTOR_BACKEND "$MODE"
set_key STOCK_CHROMA_DIR /opt/hermes-stock/shared/chroma
set_key STOCK_EXP_CHROMA_DIR /opt/hermes-stock/shared/chroma_exp
set_key STOCK_MILVUS_URI http://127.0.0.1:19530
set_key STOCK_MILVUS_TOKEN ""
set_key STOCK_RAG_MILVUS_COLLECTION knowledge_chunks
set_key STOCK_EXP_MILVUS_COLLECTION experience_entries

echo "configured $MODE in $ENV_FILE"
echo "backup: $backup"
