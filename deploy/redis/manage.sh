#!/usr/bin/env bash
set -euo pipefail

ROOT="${REDIS_ROOT:-/opt/hermes-stock/redis}"
IMAGE="${REDIS_IMAGE:-redis:8.8.0}"
MIRROR_IMAGE="${REDIS_MIRROR_IMAGE:-docker.m.daocloud.io/library/redis:8.8.0}"
CONTAINER="hermes-stock-redis"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install_redis() {
  install -d -m 0750 -o 999 -g 999 "$ROOT/data"
  install -m 0644 "$SCRIPT_DIR/redis.conf" "$ROOT/redis.conf"

  if [[ ! -s "$ROOT/connection.env" || ! -s "$ROOT/users.acl" ]]; then
    local password
    password="$(openssl rand -hex 24)"
    printf 'user default off\nuser hermes on >%s ~* &* +@all -flushall -flushdb -config -module -debug -shutdown\n' \
      "$password" > "$ROOT/users.acl"
    printf 'STOCK_AGENT_REDIS_URL=redis://hermes:%s@127.0.0.1:6379/0\n' \
      "$password" > "$ROOT/connection.env"
  fi

  chmod 600 "$ROOT/users.acl" "$ROOT/connection.env"
  chown 999:999 "$ROOT/users.acl" "$ROOT/redis.conf" "$ROOT/data"

  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker pull "$IMAGE" || {
      docker pull "$MIRROR_IMAGE"
      docker tag "$MIRROR_IMAGE" "$IMAGE"
    }
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --user 999:999 \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=32m \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --memory 2560m \
    --cpus 2 \
    --pids-limit 256 \
    --log-opt max-size=20m \
    --log-opt max-file=5 \
    -p 127.0.0.1:6379:6379 \
    -v "$ROOT/data:/data" \
    -v "$ROOT/redis.conf:/usr/local/etc/redis/redis.conf:ro" \
    -v "$ROOT/users.acl:/usr/local/etc/redis/users.acl:ro" \
    "$IMAGE" redis-server /usr/local/etc/redis/redis.conf >/dev/null
  status_redis
}

status_redis() {
  if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "Redis container is not installed."
    return 1
  fi
  # shellcheck disable=SC1090
  source "$ROOT/connection.env"
  docker inspect --format 'status={{.State.Status}} restart={{.RestartCount}} image={{.Config.Image}}' "$CONTAINER"
  docker exec "$CONTAINER" redis-cli -u "$STOCK_AGENT_REDIS_URL" --no-auth-warning PING
  docker port "$CONTAINER" 6379/tcp
}

case "${1:-status}" in
  install|upgrade)
    install_redis
    ;;
  restart)
    docker restart "$CONTAINER" >/dev/null
    status_redis
    ;;
  status)
    status_redis
    ;;
  logs)
    docker logs --tail 200 "$CONTAINER"
    ;;
  *)
    echo "Usage: $0 {install|upgrade|restart|status|logs}" >&2
    exit 2
    ;;
esac
