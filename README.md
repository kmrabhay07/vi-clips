# VI Clips

Turn long recordings into captioned 1080p clips. The project includes a Next.js dashboard, FastAPI API, PostgreSQL job queue, and a bounded parallel video worker. Docker includes FFmpeg and the Python dependencies; installing FFmpeg or Python on the host is unnecessary for normal local setup.

## Requirements

- Docker Desktop with Linux containers on Windows/macOS, or Docker Engine with Compose v2 on Linux.
- Internet for the initial container build and speech-model download.
- Free disk space for originals, extracted audio, previews, exports, and temporary merges. YouTube imports can need roughly twice the downloaded size plus working-space headroom.
- Two CPU cores and about 8 GB RAM are a practical starting point. Long recordings take longer on CPU; there is no GPU requirement.

Run all commands from the project directory.

## Quick local setup on Windows

```powershell
.\Start-Local.ps1
```

If PowerShell script execution is restricted, use the manual Docker commands below or follow your organization's policy.

On first use, this creates `env/.env` from the safe template and generates a random database password without displaying it. It then builds and starts the local stack. Existing private configuration is preserved.

Open **http://localhost:3166**. The address follows `WEB_PORT` in `env/.env` if you change it. If PowerShell blocks scripts, review the file and use your organization's permitted execution policy; the equivalent Docker commands below also work.

## Manual setup on Windows, macOS, or Linux

Copy the template if a private environment file does not already exist:

```powershell
# PowerShell
Copy-Item env/.env.example env/.env
```

```bash
# macOS / Linux
cp env/.env.example env/.env
chmod 600 env/.env
```

Edit `env/.env` and set `POSTGRES_PASSWORD` to a long, random URL-safe value. Hex characters work well. Do not overwrite an existing configured password: the PostgreSQL volume keeps the password with which it was initialized. URL-reserved characters need encoding in connection URLs.

```bash
docker compose --env-file env/.env config --quiet
docker compose --env-file env/.env up --build -d
docker compose --env-file env/.env ps
docker compose --env-file env/.env logs --tail 50 worker
```

The web service binds to loopback only. API and PostgreSQL are internal to Compose. This is a private single-user application; add authentication and HTTPS before any public exposure.

## Private configuration

| Location | Purpose | Share/commit? |
| --- | --- | --- |
| `env/.env.example` | Empty/safe configuration template | Yes |
| `env/.env` | Your database password and optional private cloud settings | No |
| `env/backups/` | Private database or migration backups | No |
| Docker `media` volume | Original videos, clips, models, and encryption key | No |
| Docker `postgres` volume | Library, jobs, settings, encrypted integrations | No |

`env/.env` and private backups are ignored by Git. Private keys remain outside the project: only their paths are configured. Do not include `env/.env`, backups, OAuth JSON files, cookie exports, or Docker-volume data when sharing an archive. Ignoring files does not remove them from an existing Git history or previously shared archive; rotate any credentials that were previously exposed.

Important settings:

| Variable | Default in template | Purpose |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | Empty; required | Database credential |
| `POSTGRES_USER`, `POSTGRES_DB` | `clips` | Database identity |
| `COMPOSE_PROJECT_NAME` | `vi-clips` | Namespaces containers and volumes; changing it opens a different library |
| `WEB_PORT` | `3166` | Local browser port |
| `APP_BASE_URL` | `http://localhost:3166` | OAuth/public app origin; keep consistent with the browser port |
| `WORKER_CONCURRENCY` | `2` | Concurrent clip jobs; do not scale coordinator replicas |
| `FFMPEG_THREADS` | `1` | Threads per video process |
| `WHISPER_MODEL` | `tiny` | Speech model downloaded on first use |
| `WHISPER_THREADS`, `WHISPER_BEAM_SIZE` | `1` | Conservative CPU settings |

Leave `SSH_*` fields empty for a fully local installation. Secrets and server addresses are read from configuration rather than embedded in launch scripts.

## First recording

1. Open New project. Upload an MP4 of at least 20 seconds, or import a YouTube URL. The maximum accepted file size is 20 GB, subject to available working space.
2. Select standard framing, or **Webcam + gameplay**. Webcam mode captures one frame inside Import so you can select and arrange both crop areas before analysis begins.
3. Analysis scans motion/audio and transcribes in five-minute sections with saved progress. Early audio-based highlights can appear while a long source is still being analyzed. Global selections are created after analysis completes.
4. Open Review clip, adjust trim/crop/captions, save a preview, then approve or reject. Final exports are 1080p; rejection and published-media cleanup follow the app's configured policy.
5. Render/download approved clips, or connect YouTube and schedule uploads. No publishing happens merely by importing footage.

Public YouTube links can still be blocked by YouTube. Optional browser-session sharing needs the provided extension's initial browser permission; a normal website cannot read another site's cookies. The browser-session extension currently recognizes localhost:3166 and 127.0.0.1:3166; keep that port when using it. Publishing OAuth is separate from downloading. You can always upload an original MP4.

## Optional YouTube publishing

Local clipping and downloads do not require a Google client.

For publishing, enable YouTube Data API v3 in your own Google Cloud project and create a Web application OAuth client. Register the exact callback shown by `/api/youtube/status`, normally `http://localhost:3166/youtube/callback`. If your consent screen is in Testing, add your Google account as a test user.

Keep the downloaded client JSON in an ignored private location such as `env/oauth/client.json`, then configure it through the API:

```powershell
curl.exe -F "file=@env/oauth/client.json" http://localhost:3166/api/youtube/config
```

On macOS/Linux use `curl` instead of `curl.exe`. Open Publishing queue and log in with YouTube. Client credentials, tokens, and download sessions are encrypted in the database; the runtime encryption key lives at `/data/.secrets/encryption.key` in the media volume. Preserve the key with the database when backing up. Do not copy runtime tokens into source files.

## Optional local frontend with a private cloud backend

This is a separate mode from fully local setup. Configure `SSH_HOST`, `SSH_USER`, `SSH_IDENTITY_FILE`, `SSH_KNOWN_HOSTS_FILE`, `LOCAL_TUNNEL_PORT`, and `CLOUD_WEB_PORT` in `env/.env`. Use an OpenSSH-format key and a verified/pinned known-hosts file.

```powershell
docker compose --env-file env/.env -f docker-compose.yml -f compose.cloud-backend.yml build web
.\Start-Frontend.ps1
```

Only the frontend runs locally in this mode. `Start-Cloud.ps1` is an alternative tunnel to the entire cloud UI; it cannot use the same local port as a running local frontend. Local and cloud databases are separate libraries. Do not activate standby backend workers or duplicate publishing queues accidentally when switching modes. See [OCI_DEPLOYMENT.md](OCI_DEPLOYMENT.md) for generic server instructions; private addresses and paths belong in environment configuration.

## Stop, logs, and updates

```bash
# Stop services while keeping all data
docker compose --env-file env/.env stop

# Rebuild after a source change
docker compose --env-file env/.env up --build -d

# Inspect service state and bounded log output
docker compose --env-file env/.env ps
docker compose --env-file env/.env logs --tail 100 api worker
```

Named volumes preserve the library. `docker compose down -v` deletes volume data; it is not a normal stop/update command. Do not delete the encryption key to resolve a login problem.

## Tests and development checks

The Docker test command uses an isolated temporary SQLite database by default; PostgreSQL-only checks are skipped:

```bash
docker compose --env-file env/.env run --rm --no-deps worker python -m unittest discover -s tests
```

For frontend checks with Node.js 24 installed:

```bash
cd web
npm ci
npm run typecheck
npm run build
```

The production frontend uses a small standalone runtime. [BACKEND_REVIEW.md](BACKEND_REVIEW.md) explains parallel processing and optimizations. [VALIDATION.md](VALIDATION.md) contains the recorded checks.

## Troubleshooting

- **Port already in use:** stop the other frontend/tunnel, or update both `WEB_PORT` and `APP_BASE_URL` and register the matching OAuth callback.
- **No clips yet:** check source progress and worker logs. Long recordings still require CPU time. Check errors and free space before retrying; re-uploading is usually unnecessary.
- **Low storage:** the app reserves working space for the OS/database. Remove unwanted sources through the app or increase storage. Do not manually remove active volumes.
- **Database authentication fails:** verify the original private password matches the existing volume. Editing the environment file does not change an initialized PostgreSQL user's password.
- **Cloud API offline:** check the SSH tunnel and pinned host/key paths. The local browser/tunnel must remain available for access; server processing continues independently.
- **YouTube verification/upload errors:** check Google test-user access, callback configuration, channel limits, and API permissions. A valid session does not guarantee YouTube accepts a server download.