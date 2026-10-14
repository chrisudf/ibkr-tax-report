# Hosted deployment — https://cgt.nomad403.cc

The public copy runs on a DigitalOcean droplet (167.71.193.42, 1 vCPU, 1 GB)
that also hosts other services. It sits behind the Caddy that belongs to the
`ibkr-portfolio` project (`/opt/ibkr-portfolio/deploy`, container
`deploy-caddy-1`) and joins that project's Docker network, `deploy_web`.

| | |
|---|---|
| Code | `/opt/ibkr-tax-report` — a clone of this repository on `main` |
| Compose | `/opt/ibkr-tax-report/deploy/docker-compose.yml`, project `ibkr-tax-report`, service `ibkr-tax-report` |
| Reached by Caddy as | `ibkr-tax-report:8000` (no host ports published) |
| DNS | Cloudflare: `A cgt → 167.71.193.42`, **DNS only (grey cloud)** |
| TLS | Caddy's automatic Let's Encrypt certificate |

The droplet's clock is Brisbane time, not UTC. Avoid deploying from 09:00 UTC
(19:00 Brisbane) for about an hour: the IBKR panel's daily sync runs then on
the same single CPU.

## What the container is allowed to do

All set in `docker-compose.yml`:

- **gunicorn**, one worker process with 4 threads (the rate limiter and the
  RBA table live in that process's memory), as a non-root user (uid 10001).
- **Read-only root filesystem.** The only writable places are tmpfs (RAM):
  `/results` (64 MB, report files), `/rba` (4 MB, refreshed rate table),
  `/tmp` (16 MB) and Docker's `/dev/shm`.
- **No volumes, no bind mounts, no Docker socket.** Nothing survives a
  restart — by design.
- **`mem_limit` = `memswap_limit` = 256 MB**: hard memory cap with no swap, so
  uploads and results never reach the droplet's swapfile. `cpus: 0.75`,
  `pids_limit: 128`.
- **`cap_drop: [ALL]`**, **`no-new-privileges`**, `restart: unless-stopped`,
  log rotation (3 × 1 MB).
- App settings (see the top of `app.py`): hosted page text, results kept 15
  minutes, 10 MB upload limit, 10 reports/minute and 60/hour per client IP,
  client IP from `X-Forwarded-For` with exactly one proxy hop (Caddy), weekly
  RBA refresh.

Caddy does not log requests for this site (no `log` directive), and gunicorn
has no access log. Neither ever sees anything but headers.

## First deploy

```bash
git clone https://github.com/chrisudf/ibkr-tax-report.git /opt/ibkr-tax-report
cd /opt/ibkr-tax-report/deploy
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

Then add the Caddy site block (next section).

## Updating

```bash
cd /opt/ibkr-tax-report && git pull --ff-only && cd deploy && \
  GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

`GIT_COMMIT` is shown next to the version in the page footer and in
`/healthz`, so you can see which commit is live. Results in `/results` are
lost on restart (they would be gone within 15 minutes anyway).

Checks:

```bash
curl -s https://cgt.nomad403.cc/healthz            # version, RBA coverage and refresh status
docker compose ps                                  # "healthy"
docker compose logs --tail 50                      # startup, RBA refresh outcomes
docker compose exec ibkr-tax-report ls -la /results    # report runs currently kept
docker stats --no-stream ibkr-tax-report-ibkr-tax-report-1
```

## Caddy

The site block, appended to `/opt/ibkr-portfolio/deploy/Caddyfile`:

```caddyfile
# ibkr-tax-report (/opt/ibkr-tax-report/deploy, same docker network). Public:
# no basic_auth. No log directive: requests are not logged.
cgt.nomad403.cc {
    encode gzip

    request_body {
        max_size 11MB
    }

    reverse_proxy ibkr-tax-report:8000
}
```

**The Caddy container does not see edits to the host file.** It bind-mounts
`/opt/ibkr-portfolio/deploy/Caddyfile` as a single file, which pins the inode
it was started with; the host file has since been replaced, so the container's
`/etc/caddy/Caddyfile` is an old copy. Reloading the default config would
reload that old copy. Instead:

```bash
F=/opt/ibkr-portfolio/deploy/Caddyfile
cp $F /root/Caddyfile.bak.$(date +%F)              # 1. back up
"$EDITOR" $F                                       # 2. edit the host file
docker cp $F deploy-caddy-1:/config/Caddyfile.next # 3. copy into the container's writable /config
docker exec deploy-caddy-1 caddy validate --config /config/Caddyfile.next --adapter caddyfile
docker exec deploy-caddy-1 caddy reload   --config /config/Caddyfile.next --adapter caddyfile
```

The reload is graceful (no dropped connections). The next time the Caddy
container is recreated it binds the current host file, which has the same
content. The Caddyfile is `skip-worktree` in the ibkr-portfolio checkout, so
`git pull` there does not overwrite it.

After any reload, check all three sites:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://nomad403.cc/        # 401 (basic auth)
curl -s -o /dev/null -w "%{http_code}\n" https://sec.nomad403.cc/    # 401 (basic auth)
curl -s -o /dev/null -w "%{http_code} %{ssl_verify_result}\n" https://cgt.nomad403.cc/   # 200 0
```

### Putting it behind a password later

Generate a bcrypt hash inside the Caddy container (the password does not end
up in a file):

```bash
docker exec -it deploy-caddy-1 caddy hash-password
```

and add a `basic_auth` block to the site, then copy, validate and reload as
above:

```caddyfile
cgt.nomad403.cc {
    encode gzip

    basic_auth /* {
        # username   bcrypt-hash
        someuser     $2a$14$...
    }

    request_body {
        max_size 11MB
    }

    reverse_proxy ibkr-tax-report:8000
}
```

Reusing the user and hash of the other two sites works too: copy their
`basic_auth` block. Nothing in the app changes — the browser sends the
credentials with every request, including the JavaScript `fetch` calls.

## RBA rate table

The image contains `data/rba_f11.csv` as of its build. The app refreshes it
in place, without a restart:

- 60 seconds after start, then every 168 hours
  (`IBKR_TAX_RBA_REFRESH_HOURS`), it downloads
  `https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv`.
- The download replaces the table in use only if it parses, starts no later
  and ends later, and matches the table in use row for row (every date and
  currency) wherever the two overlap — `rba_refresh.py`. The validated file
  is written to `/rba` (tmpfs) and swapped in for every report started after
  that.
- If it fails validation, the table in use stays and the reason is logged
  (`RBA refresh rejected: ...`). A network failure is retried after 6 hours
  instead of a week.
- `/healthz` shows `rba.last` (last date covered), `rba.source` and
  `rba.last_outcome`.

After a restart `/rba` is empty, so the app starts from the image's table and
refreshes a minute later. To move the bundled table forward, run
`python rba_refresh.py` locally and commit `data/rba_f11.csv`.

Connectivity was checked from the droplet on 2026-10-15: both the host and a
container on `deploy_web` fetched the file (HTTP 200, 141 KB) with the app's
User-Agent — the RBA does not block this data-centre address.
