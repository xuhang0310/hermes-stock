# Workspace Python sandbox

The API never receives Docker socket access. A separate `hermes-sandbox` account
runs `backend.app.execution.broker` and exposes only a Unix socket owned by the
`hermes` group. The broker accepts a user id, a relative `.py` path and a timeout;
all Docker options are fixed in code.

Each application user has a reusable container object. It is started for one run
and stopped immediately afterwards, so detached child processes cannot survive.
The only writable host mount is that user's workspace. Networking is disabled,
the root filesystem is read-only, all Linux capabilities are dropped, and cgroup,
PID, file-size, output and time limits are applied.

Build from the repository root:

```bash
docker build -f deploy/sandbox/Dockerfile -t hermes-stock-python-sandbox:latest .
```

Install the service after creating the dedicated account:

```bash
useradd --system --no-create-home --shell /sbin/nologin hermes-sandbox
usermod -aG docker hermes-sandbox
install -m 0644 deploy/systemd/hermes-stock-sandboxd.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hermes-stock-sandboxd
```
