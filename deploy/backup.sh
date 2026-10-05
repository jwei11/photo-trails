#!/bin/sh
# Nightly database backup to the bucket (run by cron, see docs/deploy.md). Place data
# isn't included: it's reloaded from GeoNames by the geodata service after a restore.
set -eu
cd "$(dirname "$0")"
dump=$(mktemp /tmp/trip-mapper-XXXXXX.dump)
trap 'rm -f "$dump"' EXIT

docker compose exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --exclude-table-data=places --exclude-table-data=admin1_regions --exclude-table-data=countries' > "$dump"
docker compose run --rm --no-deps -T api python -m app.backup upload < "$dump"
docker compose run --rm --no-deps -T api python -m app.backup prune
