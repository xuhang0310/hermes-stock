#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${MILVUS_DATA_DIR:-/opt/hermes-stock/shared/milvus}"
NETWORK="hermes-milvus"

exists() {
  docker inspect "$1" >/dev/null 2>&1
}

wait_healthy() {
  local name="$1"
  local attempts="${2:-60}"
  local status
  for ((i = 1; i <= attempts; i++)); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name")"
    if [[ "$status" == "healthy" ]]; then
      return 0
    fi
    if [[ "$status" == "unhealthy" || "$status" == "exited" || "$status" == "dead" ]]; then
      docker logs --tail 80 "$name" >&2 || true
      return 1
    fi
    sleep 3
  done
  echo "$name did not become healthy in time" >&2
  docker logs --tail 80 "$name" >&2 || true
  return 1
}

wait_etcd() {
  for ((i = 1; i <= 40; i++)); do
    if docker exec milvus-etcd etcdctl endpoint health >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  echo "milvus-etcd did not become ready in time" >&2
  docker logs --tail 80 milvus-etcd >&2 || true
  return 1
}

up() {
  install -d -m 0755 "$DATA_DIR/etcd" "$DATA_DIR/minio" "$DATA_DIR/standalone"
  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

  if exists milvus-etcd; then
    docker start milvus-etcd >/dev/null
  else
    docker run -d --name milvus-etcd --network "$NETWORK" --restart unless-stopped \
      -e ETCD_AUTO_COMPACTION_MODE=revision \
      -e ETCD_AUTO_COMPACTION_RETENTION=1000 \
      -e ETCD_QUOTA_BACKEND_BYTES=4294967296 \
      -e ETCD_SNAPSHOT_COUNT=50000 \
      -v "$DATA_DIR/etcd:/etcd" \
      quay.io/coreos/etcd:v3.5.25 \
      etcd --advertise-client-urls=http://127.0.0.1:2379 \
      --listen-client-urls=http://0.0.0.0:2379 --data-dir=/etcd >/dev/null
  fi

  if exists milvus-minio; then
    docker start milvus-minio >/dev/null
  else
    docker run -d --name milvus-minio --network "$NETWORK" --restart unless-stopped \
      -e MINIO_ACCESS_KEY=minioadmin -e MINIO_SECRET_KEY=minioadmin \
      -v "$DATA_DIR/minio:/minio_data" \
      --health-cmd 'curl -f http://localhost:9000/minio/health/live || exit 1' \
      --health-interval 10s --health-timeout 5s --health-retries 10 \
      minio/minio:RELEASE.2024-12-18T13-15-44Z \
      minio server /minio_data --console-address :9001 >/dev/null
  fi

  wait_etcd
  wait_healthy milvus-minio 40

  if exists milvus-standalone; then
    docker start milvus-standalone >/dev/null
  else
    docker run -d --name milvus-standalone --network "$NETWORK" --restart unless-stopped \
      --security-opt seccomp=unconfined --ulimit memlock=-1:-1 \
      -e ETCD_ENDPOINTS=milvus-etcd:2379 -e MINIO_ADDRESS=milvus-minio:9000 -e MQ_TYPE=woodpecker \
      -v "$DATA_DIR/standalone:/var/lib/milvus" \
      -p 127.0.0.1:19530:19530 -p 127.0.0.1:9091:9091 \
      --health-cmd 'curl -f http://localhost:9091/healthz || exit 1' \
      --health-interval 10s --health-start-period 90s --health-timeout 5s --health-retries 12 \
      milvusdb/milvus:v2.6.19 milvus run standalone >/dev/null
  fi

  wait_healthy milvus-standalone 80
  status
}

status() {
  docker ps -a --filter 'name=milvus-' \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
}

stop() {
  docker stop -t 90 milvus-standalone >/dev/null 2>&1 || true
  docker stop -t 30 milvus-minio milvus-etcd >/dev/null 2>&1 || true
}

case "${1:-status}" in
  up) up ;;
  status) status ;;
  stop) stop ;;
  logs) docker logs --tail "${2:-200}" milvus-standalone ;;
  *) echo "usage: $0 {up|status|stop|logs [lines]}" >&2; exit 2 ;;
esac
