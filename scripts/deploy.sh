#!/bin/bash
set -euo pipefail
# Install a reviewed commit as an immutable local preview. Does not touch main.
cd "$(dirname "$0")/.."
release_sha=$(git rev-parse --verify "${1:-HEAD}^{commit}")
release_dir="/opt/marina/releases/$release_sha"
if [ -e "$release_dir" ]; then
  echo 'Release already exists; refusing to overwrite it.' >&2
  exit 1
fi
if [ ! -f /etc/marina.env ]; then
  echo 'Configure /etc/marina.env with mode 0600 before deploying.' >&2
  exit 1
fi
install -d -m 0755 "$release_dir"
git archive "$release_sha" | tar -x -C "$release_dir"
python3 -m virtualenv "$release_dir/.venv"
"$release_dir/.venv/bin/pip" install -r "$release_dir/requirements.lock"
cd "$release_dir"
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
fi
for unit in "$release_dir"/deploy/marina-*; do
  install -m 0644 "$unit" /etc/systemd/system/
done
ln -s "$release_dir" /opt/marina/current.next
mv -Tf /opt/marina/current.next /opt/marina/current
systemctl daemon-reload
systemctl restart marina-web marina-poll marina-worker
systemctl enable marina-backup.timer
printf '%s\n' "$release_sha" > /var/lib/marina/deployed-commit
chmod 0644 /var/lib/marina/deployed-commit
echo "Installed preview commit $release_sha. Verify /health and service status."
