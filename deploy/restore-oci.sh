#!/usr/bin/env bash
set -euo pipefail
root=${VI_DEPLOY_ROOT:?Set VI_DEPLOY_ROOT to your private server deployment directory}
cd "$root/app"
compose=(sudo -n docker compose -p vi-clips-oci --env-file "$root/.env" -f docker-compose.yml -f compose.oci.yml)
test -s "$root/backups/library.dump"
test -s "$root/backups/media.tar"
"${compose[@]}" up --no-build -d db
for attempt in $(seq 1 30); do
  if "${compose[@]}" exec -T db pg_isready -U clips >/dev/null; then break; fi
  sleep 2
done
# Restore only into the isolated, empty OCI app database.
count=$("${compose[@]}" exec -T db psql -U clips -d clips -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
if [ "$count" != 0 ]; then
  echo 'Refusing to overwrite an existing OCI application database.' >&2
  exit 1
fi
cat "$root/backups/library.dump" | "${compose[@]}" exec -T db pg_restore -U clips -d clips --no-owner --no-acl --exit-on-error
sudo -n docker run --rm --network none -v vi-clips-oci_media:/data -v "$root/backups:/backup:ro" vi-clips-backend:oci python -c "import tarfile; from pathlib import Path; root=Path('/data'); archive=tarfile.open('/backup/media.tar'); archive.extractall(root,filter='data'); files=[m for m in archive.getmembers() if m.isfile()]; assert all((root/m.name).stat().st_size==m.size for m in files); print('Restored and size-verified',len(files),'media/configuration files')"
"${compose[@]}" up --no-build -d api web
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:3166/api/health >/dev/null; then break; fi
  sleep 2
done
curl -fsS http://127.0.0.1:3166/api/health
"${compose[@]}" exec -T api python -c "from db import Session,Source,Clip,Publication; from sqlalchemy import select,func; from youtube_auth import connection_status; from youtube_cookies import cookie_status; import json; s=Session(); print(json.dumps({'sources':s.scalar(select(func.count()).select_from(Source)),'clips':s.scalar(select(func.count()).select_from(Clip)),'publications':s.scalar(select(func.count()).select_from(Publication)),'youtube_connected':connection_status()['connected'],'import_cookies_configured':cookie_status()['configured']}))"
df -h /
echo 'RESTORE VERIFIED. Worker is intentionally not started until cutover verification.'
