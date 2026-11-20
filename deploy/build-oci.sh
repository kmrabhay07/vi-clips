#!/usr/bin/env bash
set -euo pipefail
root=${VI_DEPLOY_ROOT:?Set VI_DEPLOY_ROOT to your private server deployment directory}
mkdir -p "$root/app" "$root/backups"
chmod 700 "$root" "$root/backups"
tar -xzf "$root/releases/source.tar.gz" -C "$root/app"
export VI_DEPLOY_ROOT
python3 - <<'PY'
from pathlib import Path
import os, secrets
path=Path(os.environ['VI_DEPLOY_ROOT'])/'.env'
if not path.exists():
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as file:
        file.write('POSTGRES_PASSWORD='+secrets.token_urlsafe(40)+'\nWEB_PORT=3166\nAPP_BASE_URL=http://localhost:3166\nWORKER_CONCURRENCY=1\nFFMPEG_THREADS=1\nWHISPER_THREADS=1\nWHISPER_BEAM_SIZE=1\n')
PY
cd "$root/app"
sudo -n docker compose -p vi-clips-oci --env-file "$root/.env" -f docker-compose.yml -f compose.oci.yml config --quiet
sudo -n docker compose -p vi-clips-oci --env-file "$root/.env" -f docker-compose.yml -f compose.oci.yml build api web
df -h /
