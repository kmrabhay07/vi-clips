#!/usr/bin/env bash
set -euo pipefail
umask 077
root=${VI_DEPLOY_ROOT:?Set VI_DEPLOY_ROOT to your private server deployment directory}
folder="$root/backups/daily"
mkdir -p "$folder"
cd "$root/app"
compose=(sudo -n docker compose -p vi-clips-oci --env-file "$root/.env" -f docker-compose.yml -f compose.oci.yml)
stamp=$(date -u +%Y%m%d-%H%M%S)
"${compose[@]}" exec -T db pg_dump -U clips -d clips -Fc --no-owner --no-acl > "$folder/$stamp.dump.partial"
mv "$folder/$stamp.dump.partial" "$folder/$stamp.dump"
sudo -n docker run --rm --network none -v vi-clips-oci_media:/data:ro vi-clips-backend:oci python -c "import tarfile,sys; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); t.add('/data/.secrets',arcname='.secrets'); t.close()" > "$folder/$stamp-key.tar.gz"
# Retain the initial complete migration snapshot; expire only this job's daily metadata backups.
find "$folder" -maxdepth 1 -type f \( -name '*.dump' -o -name '*-key.tar.gz' \) -mtime +7 -delete
echo 'Database and encryption-key backup completed. Media remains on its persistent volume.'
