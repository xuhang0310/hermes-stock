# Redis deployment

Redis is a coordination layer for Agent run queues, event wake-ups, and
distributed leases. MySQL remains the durable source of truth, so a Redis
outage slows polling but does not lose accepted tasks.

The production container is pinned to Redis 8.8.0, listens only on the host
loopback interface, persists AOF data, runs without Linux capabilities, and
uses an ACL user instead of the default user.

## Install

Copy this directory to the server, then run as root:

```bash
bash manage.sh install
```

The script uses plain `docker run`, matching the current CentOS host; Docker
Compose is not required. Existing credentials and data are retained during an
upgrade.

Install the systemd units from `deploy/systemd/` and run
`systemctl daemon-reload`. Both API replicas read `connection.env` for event
stream wake-ups and queue submission; only `hermes-stock-background` starts
Agent workers.

## Verify

```bash
bash manage.sh status
ss -lntp | grep 127.0.0.1:6379
```

Do not publish port 6379 publicly. Keep `users.acl`, `connection.env`, and the
data directory outside release folders so deployments and rollbacks retain
the queue configuration and persisted state.
