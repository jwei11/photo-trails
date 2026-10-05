#!/bin/sh
# Run a given commit's image: ./deploy.sh <full commit sha>
# Pulls first, so a wrong or unpublished tag fails before anything stops. Migrations run
# (in `migrate`) before the new API and worker start. Rolling back is deploying an older
# SHA, as long as no migration since then has removed something it needs.
set -eu
cd "$(dirname "$0")"
sha="${1:?usage: ./deploy.sh <full commit sha>}"
image="ghcr.io/jwei11/photo-trails:$sha"

docker pull "$image"
# Point .env at the new image (the only line that changes on a deploy).
sed -i "s|^APP_IMAGE=.*|APP_IMAGE=$image|" .env
# Everything (the first deploy also starts db, migrate, geodata), then wait for the
# long-running services to report healthy.
docker compose up -d
docker compose up -d --wait api worker caddy
docker compose ps
# Old images take disk; keep only what's in use.
docker image prune -af --filter "until=168h" > /dev/null
echo "Deployed $sha"
