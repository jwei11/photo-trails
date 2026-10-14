# Deploying (M12, pass A: by hand)

The app runs on one DigitalOcean droplet with Docker: Caddy (HTTPS) in front of the API,
the worker, and Postgres, all from `deploy/compose.yaml`. Photos and database backups are
in an S3 bucket. The address is a DuckDNS name. These are the notes from doing it by hand;
pass B turns them into Terraform.

```
browser ──HTTPS──▶ Caddy :443 ──▶ api ──▶ db (Postgres + PostGIS, volume)
   │                               │  worker ──┘
   └──── presigned URLs ─────▶ S3 bucket ◀──┘  (photos, and backups/ nightly)
```

## 1. Before any server exists (in the browser)

1. **DigitalOcean account.** If you have the GitHub Student Developer Pack, claim its
   DigitalOcean credit first. Then, before creating anything: Settings → Billing →
   **Billing alerts**, alert at $10 (the droplet is $6/month).
2. **DuckDNS name.** At duckdns.org, sign in with GitHub and add a subdomain, e.g.
   `tripmapper-jw`. That's `DOMAIN=tripmapper-jw.duckdns.org`. Leave the IP for now.
3. **Production S3 bucket** (AWS console, us-east-2), like the dev one but separate:
   - Bucket `trip-mapper-prod-<initials>`, Block all public access on.
   - CORS: allow `GET` and `PUT` from `https://<DOMAIN>`, header `content-type`.
   - IAM user `trip-mapper-prod` with only `s3:ListBucket` on the bucket and
     `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `arn:aws:s3:::<bucket>/*`.
     Create an access key; it goes in the server's `.env` (step 3 below), nowhere else.
4. **Google sign-in.** In the OAuth client from development, add the redirect URI
   `https://<DOMAIN>/api/auth/google/callback`.
5. **A token for pulling the image.** The repo is private, so its images are too. GitHub →
   Settings → Developer settings → Personal access tokens (classic) → scope
   `read:packages` only, expiry one year. The server logs in with it once (step 3).
6. **SSH key.** On your Mac, `ls ~/.ssh/id_ed25519.pub`; if missing, `ssh-keygen -t ed25519`.

## 2. The droplet

- Create droplet: Ubuntu 24.04 LTS, Basic, Regular, **1 GB / 1 vCPU ($6)**, region
  **NYC** (close to the bucket in us-east-2: the worker downloads every original), SSH key
  authentication with the key above (no password), monitoring on. Name `trip-mapper-1`.
- Networking → Firewalls → create `trip-mapper`: inbound TCP 22, 80, 443 and UDP 443
  (HTTP/3); outbound everything. Apply it to the droplet. Docker publishes ports past the
  server's own firewall (ufw), so this cloud firewall is the one that counts.
- DuckDNS: set the subdomain's IP to the droplet's public IPv4.

What went wrong the first time, so it doesn't again:
- The firewall needs the HTTP rule (port 80) too. Without it HTTPS still works (Caddy
  proves the domain over port 443), but `http://` addresses just time out.
- The server's `.env` must have the **prod** IAM user's keys. With the dev user's, the API
  refuses to start ("Can't reach bucket ... credentials are wrong"). To see whose keys
  they are: `docker compose run --rm --no-deps -T api python -c "import boto3; from app
  import config as c; print(boto3.client('sts', aws_access_key_id=c.S3_ACCESS_KEY,
  aws_secret_access_key=c.S3_SECRET_KEY).get_caller_identity()['Arn'])"`.

## 3. On the server

```bash
ssh root@<ip>
# Updates, and security updates from now on (on by default in Ubuntu; check it's there).
apt-get update && apt-get -y upgrade && apt-get -y install unattended-upgrades
# 2 GB of swap: the droplet has 1 GB, and loading place data briefly needs more.
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
# Docker, from Docker's own repository (Ubuntu's is older).
curl -fsSL https://get.docker.com | sh
# SSH: keys only (DigitalOcean's default when created with a key; confirm).
grep -ri '^PasswordAuthentication' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/
mkdir -p /opt/trip-mapper
```

From the repo on your Mac: `scp deploy/compose.yaml deploy/Caddyfile deploy/*.sh deploy/.env.example root@<ip>:/opt/trip-mapper/`

Back on the server:

```bash
cd /opt/trip-mapper
cp .env.example .env && chmod 600 .env && nano .env   # fill in everything
docker login ghcr.io -u <github user>                 # paste the read:packages token
./deploy.sh <full sha of master's latest commit>      # first start: also loads place data
docker compose exec api python -m app.users create you@example.com
echo '15 3 * * * root /opt/trip-mapper/backup.sh >> /var/log/trip-mapper-backup.log 2>&1' > /etc/cron.d/trip-mapper-backup
./backup.sh && docker compose run --rm --no-deps -T api python -m app.backup list
```

## 4. Check it

- `https://<DOMAIN>` loads with a valid certificate, and `http://` redirects to it.
- Sign in with Google; import a photo; it gets a thumbnail and a place.
- Share a trip and open the link on a phone on mobile data, signed out.
- `curl -sI https://<DOMAIN>/api/photos` is 401.

## Deploying a new version

`ssh root@<ip> /opt/trip-mapper/deploy.sh <commit sha>`, with a SHA CI has published from
master. Rolling back is the same with an older SHA.

## The demo, and sign-ups

"Try the demo" on the sign-in page appears once a demo trip is loaded. Copy a folder of
photos to the server and load it (run again with `--replace` to swap in a new set):

```bash
scp -r <folder> root@<ip>:/opt/trip-mapper/demo-trip     # from the Mac
chmod -R a+rX /opt/trip-mapper/demo-trip                # the container's user must read it
cd /opt/trip-mapper
docker compose run --rm --no-deps -v /opt/trip-mapper/demo-trip:/demo:ro api \
    python -m app.demo load /demo --name "Death Valley and Sequoia"
docker compose run --rm --no-deps -T api python -m app.demo status
```

Loading processes every photo in that one-off container (37 large photos: a few minutes,
~350 MB of memory at peak). Guests then cost nothing to start: their copies reuse the
demo's files. `GUESTS_MAX` in `.env` caps live guests.

Sign-ups: set `SIGNUPS_MAX` in `.env` (and run `deploy.sh` again so the API picks it up),
and in the Google Cloud console move the OAuth app from Testing to **In production**
(Google Auth Platform → Audience → Publish app). Asking only for `openid email`, it needs
no review. While it's in Testing, only listed test users can sign in at all.

## Restoring a backup

```bash
cd /opt/trip-mapper
docker compose run --rm --no-deps -T api python -m app.backup list
docker compose run --rm --no-deps -T api python -m app.backup download backups/<name>.dump > /tmp/restore.dump
docker compose stop api worker
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T db sh -c 'pg_restore --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < /tmp/restore.dump
docker compose up -d   # migrate brings the schema up to date; geodata reloads place data
```
