#!/bin/bash
set -euo pipefail
# Install a reviewed commit as an immutable local preview. Does not touch main.
exec 9>/run/lock/marina-deploy.lock
flock -n 9 || { echo 'Another Marina deployment is running.' >&2; exit 1; }
cd "$(dirname "$0")/.."
release_sha=$(git rev-parse --verify "${1:-HEAD}^{commit}")
release_dir="/opt/marina/releases/$release_sha"
previous_release=$(realpath /opt/marina/current 2>/dev/null || true)
if [ -n "${MARINA_EXPECTED_CURRENT:-}" ] && [ "$previous_release" != "$MARINA_EXPECTED_CURRENT" ]; then
  echo 'Current release changed after the CI check; retry on the next timer.' >&2
  exit 1
fi
if [ -e "$release_dir" ]; then
  echo 'Release already exists; refusing to overwrite it.' >&2
  exit 1
fi
if [ ! -f /etc/marina.env ]; then
  echo 'Configure /etc/marina.env with mode 0600 before deploying.' >&2
  exit 1
fi
services_stopped=0
activated=0
recover() {
  status=$?
  if [ "$status" -ne 0 ]; then
    if [ "$activated" -eq 1 ] && [ -d "$previous_release" ]; then
      ln -s "$previous_release" /opt/marina/current.rollback
      mv -Tf /opt/marina/current.rollback /opt/marina/current
    fi
    if [ "$services_stopped" -eq 1 ]; then
      systemctl start marina-web marina-poll marina-worker || true
    fi
  fi
  exit "$status"
}
trap recover EXIT
install -d -m 0755 "$release_dir"
git archive "$release_sha" | tar -x -C "$release_dir"
python3 -m virtualenv "$release_dir/.venv"
"$release_dir/.venv/bin/pip" install -r "$release_dir/requirements.lock"
cd "$release_dir"
if [ -d "$previous_release" ]; then
  systemctl stop marina-web marina-poll marina-worker
  services_stopped=1
fi
if [ -f /var/lib/marina/marina.sqlite ]; then
  DATABASE=/var/lib/marina/marina.sqlite "$release_dir/.venv/bin/python" - <<'PY'
from datetime import datetime, timezone
from marina.operations import backup
from pathlib import Path
import os
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
target='/var/lib/marina/backups/pre-deploy-'+stamp+'.sqlite'
backup('/var/lib/marina/marina.sqlite',target)
print('Pre-deployment backup verified')
PY
  "$release_dir/.venv/bin/python" scripts/publish_tripwire_delivery.py \
    --database /var/lib/marina/marina.sqlite --env-file /etc/marina.env \
    --manifest /var/lib/marina/content/tripwire-delivery-publication-20261106.json --apply --once
fi
for unit in "$release_dir"/deploy/marina-*; do
  install -m 0644 "$unit" /etc/systemd/system/
done
if [ -n "${MARINA_EXPECTED_CURRENT:-}" ] && [ "$(realpath /opt/marina/current)" != "$MARINA_EXPECTED_CURRENT" ]; then
  echo 'Another operator activated a release during preparation; refusing to overwrite it.' >&2
  exit 1
fi
ln -s "$release_dir" /opt/marina/current.next
mv -Tf /opt/marina/current.next /opt/marina/current
activated=1
systemctl daemon-reload
systemctl restart marina-web marina-poll marina-worker
for attempt in $(seq 1 20); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:8075/health >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --max-time 3 http://127.0.0.1:8075/health >/dev/null
for unit in marina-web marina-poll marina-worker; do
  systemctl is-active --quiet "$unit"
done
systemctl enable marina-backup.timer
printf '%s\n' "$release_sha" > /var/lib/marina/deployed-commit
chmod 0644 /var/lib/marina/deployed-commit
install -m 0700 "$release_dir/scripts/autodeploy.py" /opt/marina/autodeploy.py
systemctl enable --now marina-autodeploy.timer
echo "Installed preview commit $release_sha. Verify /health and service status."
