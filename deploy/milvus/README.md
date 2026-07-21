# Milvus Standalone

Production vector storage for Hermes Stock.

- Milvus: `2.6.19`
- Python SDK: `pymilvus 2.6.16`
- Data: `/opt/hermes-stock/shared/milvus`
- API: `127.0.0.1:19530` only
- Health: `127.0.0.1:9091/healthz` only
- MinIO and etcd are reachable only on the private Docker network.

```bash
cd /opt/hermes-stock/milvus
docker compose up -d
docker compose ps
curl -f http://127.0.0.1:9091/healthz
```

The equivalent native Docker manager is available when the Compose plugin is
not installed:

```bash
cd /opt/hermes-stock/milvus
./manage.sh up
./manage.sh status
```

Stop services without deleting persisted data:

```bash
docker compose stop
# or: ./manage.sh stop
```

Do not run `docker compose down -v` in production. Back up the whole
`/opt/hermes-stock/shared/milvus` directory only after stopping the stack or by
using a storage-level snapshot with filesystem consistency.

Switch the application backend after a verified migration. The command creates
a timestamped copy under `/opt/hermes-stock/shared/env-backups` and does not
restart services:

```bash
./configure-backend.sh milvus
# rollback configuration:
./configure-backend.sh chroma
```
