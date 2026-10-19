# Hosting on Google Cloud: one VM

This runbook puts the protocol service on one Compute Engine VM, reachable as an HTTPS API (REST and MCP) that other
tools call with an API key. It uses the same image and compose setup as a laptop ([DEPLOY.md](../DEPLOY.md)), plus:

- **Caddy** in front ([deploy/gcp/Caddyfile](../deploy/gcp/Caddyfile)):
  - Gets a free HTTPS certificate (Let's Encrypt) and renews it automatically.
  - Lets a request through only with the header `Authorization: Bearer <API key>`. `/health` stays open for uptime
    checks; anything else gets 401.
- **A faster machine:** c4-standard-8 (8 vCPU, 30 GB) with `CONVERT_WORKERS=2`, two 15-page conversion batches at a
  time, and a 13 GB memory limit ([deploy/gcp/docker-compose.gcp.yml](../deploy/gcp/docker-compose.gcp.yml)).
- **A separate data disk** for the registry, documents and caches, with daily snapshots.
- **A host name without buying a domain:** `<ip with dashes>.sslip.io`. sslip.io is a public DNS service that answers
  that name with the IP in it (34-1-2-3.sslip.io gives 34.1.2.3); the traffic itself never passes through it.

```
your tools / agents ──HTTPS + API key──► Caddy :443 ──► protocol-service 127.0.0.1:8000 ──► data disk /srv/protocol-data
                                          (one VM: c4-standard-8, Ubuntu 24.04, Docker)
```

The service, the outputs and the pipeline fingerprint are the same as on a laptop. Only the ports 80 and 443 are open
to the internet; SSH goes through Google's IAP tunnel, and the service port stays on 127.0.0.1.

**Cost.** c4-standard-8 is about $0.40 an hour on demand in us-central1, about $290 a month if it runs all the time.
Disks, snapshots and the static IP add a few to a few tens of dollars a month. Stopping the VM when it is not needed
saves in proportion. The OpenRouter costs are the same as on a laptop. Check the
[pricing calculator](https://cloud.google.com/products/calculator) for your region.

**Data.** Upload only protocols you are allowed to store on Google Cloud and send to OpenRouter from there
(zero-data-retention routing, as on a laptop). This runbook starts with an empty service and does not copy the
laptop's data volume.

## Phase 1: Google Cloud (Cloud Shell)

Open [Cloud Shell](https://shell.cloud.google.com) and select a project with billing enabled in the top bar. A project
used only for this service is simplest: step 1.4 changes firewall rules for the whole project. Every command in this
phase runs in Cloud Shell, which has `gcloud` installed and signed in.

### 1.1 Variables

Run this block in every new Cloud Shell session. The IP and site lines are empty until step 1.3 has created the
address.

```bash
PROJECT_ID=$(gcloud config get-value project)
REGION=us-central1
ZONE=us-central1-a
VM=protocol-service
IP=$(gcloud compute addresses describe protocol-ip --region=$REGION --format='value(address)' 2>/dev/null)
SITE="$(echo $IP | tr . -).sslip.io"
echo "project $PROJECT_ID | zone $ZONE | IP $IP | site https://$SITE"
```

### 1.2 APIs and a budget alert

```bash
gcloud services enable compute.googleapis.com monitoring.googleapis.com iap.googleapis.com
```

In the console, open **Billing → Budgets & alerts → Create budget**, for example $350 a month with email alerts at
50, 90 and 100 %.

### 1.3 Static IP

```bash
gcloud compute addresses create protocol-ip --region=$REGION
```

Then run the 1.1 block again: it now prints the IP and the site name, for example `https://34-1-2-3.sslip.io`.

### 1.4 Firewall

The first rule opens HTTP and HTTPS. The second allows SSH only from Google's IAP range.

```bash
gcloud compute firewall-rules create protocol-allow-web --network=default --direction=INGRESS --action=ALLOW --rules=tcp:80,tcp:443,udp:443 --source-ranges=0.0.0.0/0 --target-tags=protocol-web
gcloud compute firewall-rules create protocol-allow-iap-ssh --network=default --direction=INGRESS --action=ALLOW --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=protocol-web
```

The default network's rules `default-allow-ssh` and `default-allow-rdp` open SSH and RDP to the whole internet for
every VM of the project. In a project used only for this service, delete them (an error only means they do not exist):

```bash
gcloud compute firewall-rules delete default-allow-ssh default-allow-rdp --quiet
```

### 1.5 The VM and its data disk

```bash
gcloud compute instances create $VM --zone=$ZONE --machine-type=c4-standard-8 --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-size=50GB --boot-disk-type=hyperdisk-balanced --create-disk=name=protocol-data,size=100GB,type=hyperdisk-balanced,device-name=protocol-data,auto-delete=no --network-interface=network=default,nic-type=GVNIC,address=$IP --tags=protocol-web --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
```

- The data disk is not deleted with the VM (`auto-delete=no`).
- If the zone has no c4 capacity, set `ZONE` to us-central1-b, -c or -f and run the command again. The static IP is
  regional, so it still fits.

### 1.6 Daily snapshots of the data disk

Snapshots are kept 14 days. The start time is UTC.

```bash
gcloud compute resource-policies create snapshot-schedule protocol-daily --region=$REGION --daily-schedule --start-time=03:00 --max-retention-days=14 --on-source-disk-delete=keep-auto-snapshots
gcloud compute disks add-resource-policies protocol-data --zone=$ZONE --resource-policies=protocol-daily
```

## Phase 2: On the VM

Connect from Cloud Shell. The first time, gcloud creates an SSH key: accept and press Enter for an empty passphrase.

```bash
gcloud compute ssh $VM --zone=$ZONE --tunnel-through-iap
```

The prompt changes to the VM. Every command in this phase runs there.

### 2.1 Data disk (once: this formats it)

Run this block only on the first setup: `mkfs` erases the disk.

```bash
DISK=/dev/disk/by-id/google-protocol-data
sudo mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard $DISK
sudo mkdir -p /srv/protocol-data
echo "$DISK /srv/protocol-data ext4 discard,defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /srv/protocol-data
sudo chown 10001:10001 /srv/protocol-data
df -h /srv/protocol-data
```

uid 10001 is the user the service runs as in its container.

### 2.2 Docker

This installs Docker Engine and the compose plugin from Docker's apt repository.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
exit
```

`exit` ends the SSH session so the docker group applies. Connect again with the `gcloud compute ssh` command above.
In a new Cloud Shell session, run the 1.1 block first.

### 2.3 The code, with a read-only deploy key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ingest_retrieve -N "" -C "protocol-service VM"
cat ~/.ssh/ingest_retrieve.pub
```

On GitHub, open the repository **roopchandvanga/ingest_retrieve → Settings → Deploy keys → Add deploy key**. Paste the
printed line, use the title `GCP VM`, and leave **Allow write access** off. Then:

```bash
printf 'Host github-ingest\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/ingest_retrieve\n  IdentitiesOnly yes\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
git clone github-ingest:roopchandvanga/ingest_retrieve.git ~/ingest_retrieve
cd ~/ingest_retrieve && git log --oneline -1
```

The first connection asks you to confirm GitHub's host key. Its ED25519 fingerprint is
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`; check it against
[GitHub's published fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
before answering `yes`.

### 2.4 Settings (.env)

```bash
cd ~/ingest_retrieve
cp .env.example .env
chmod 600 .env
IP=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)
SITE="$(echo $IP | tr . -).sslip.io"
printf '\n# Google Cloud VM (docs/hosting_gcp.md)\nCOMPOSE_FILE=docker-compose.yml:deploy/gcp/docker-compose.gcp.yml\nCONVERT_WORKERS=2\nALLOWED_HOSTS=%s,127.0.0.1,localhost\nPROXY_SITE=%s\nPROXY_API_KEY=%s\n' "$SITE" "$SITE" "$(openssl rand -hex 32)" >> .env
nano .env
```

What this sets:

- `COMPOSE_FILE`: plain `docker compose ...` uses both compose files.
- `CONVERT_WORKERS=2`: two conversion batches at a time.
- `ALLOWED_HOSTS`: the site name plus 127.0.0.1, which the healthcheck uses.
- `PROXY_SITE` and `PROXY_API_KEY`: Caddy's host name and a random 64-character API key.

In nano, put your OpenRouter key after `OPENROUTER_API_KEY=`, then save with Ctrl+O, Enter and quit with Ctrl+X. Then
print the API key:

```bash
grep '^PROXY_API_KEY=' ~/ingest_retrieve/.env
```

Keep it in your password manager: every client needs it. It never leaves the VM otherwise.

### 2.5 Build and start

```bash
cd ~/ingest_retrieve
python3 scripts/check_build_context.py
docker build -t protocol-service .
docker compose up -d
docker compose ps
```

- The build takes about 15–25 minutes the first time and downloads about 2 GB (Python packages, models).
- `up` downloads the Caddy image.
- Wait until `docker compose ps` shows protocol-service `(healthy)`.
- `docker compose logs caddy` should then show `certificate obtained successfully` for your site.

### 2.6 First check, on the VM

```bash
cd ~/ingest_retrieve
SITE=$(grep '^PROXY_SITE=' .env | cut -d= -f2)
KEY=$(grep '^PROXY_API_KEY=' .env | cut -d= -f2)
curl -s https://$SITE/health; echo
curl -s -o /dev/null -w "without the key: %{http_code}\n" https://$SITE/version
curl -s -H "Authorization: Bearer $KEY" https://$SITE/version | head -c 200; echo
```

Expected:

1. `{"status":"ok", ...}`
2. `without the key: 401`
3. The version JSON, with `"pipeline_version"` and `"fingerprint"`.

## Phase 3: Use it from your laptop and tools

### 3.1 URL and key in the session

Run this in PowerShell in the project folder. Paste the site URL (`https://...sslip.io`) and the key when asked.

```powershell
$env:PROTOCOL_SERVICE_URL = Read-Host 'Service URL'
$env:PROTOCOL_SERVICE_API_KEY = Read-Host 'API key'
$h = @{ Authorization = "Bearer $env:PROTOCOL_SERVICE_API_KEY" }
Invoke-RestMethod "$env:PROTOCOL_SERVICE_URL/version" -Headers $h | Select-Object pipeline_version, fingerprint, offline
```

### 3.2 A first document (REST)

This uploads the public LZZT protocol. The service then sends, with zero-data-retention routing, its figure images,
the lines of its first three pages, and its chunk texts to OpenRouter. HTTP 202 means the job is queued, not done:
the loop polls it until it ends.

```powershell
$r = curl.exe -s -H "Authorization: Bearer $env:PROTOCOL_SERVICE_API_KEY" -F "file=@public_protocols\LZZT_CDISC_Pilot\protocol.pdf" "$env:PROTOCOL_SERVICE_URL/documents" | ConvertFrom-Json
$r | Select-Object doc_id, job_id, status
do { Start-Sleep 20; $j = Invoke-RestMethod "$env:PROTOCOL_SERVICE_URL/jobs/$($r.job_id)" -Headers $h; "{0} {1} {2}/{3}" -f $j.state, $j.stage, $j.progress.done, $j.progress.total } until ($j.state -in 'done', 'done_with_warnings', 'failed', 'cancelled')
$j.stages | Select-Object name, status, seconds
```

The last line shows the seconds of every stage. On the laptop, LZZT (97 pages) took about 14 minutes end to end.

### 3.3 The automated smoke test

This checks REST and MCP over HTTP against the hosted service. It uploads the public LZZT and Lilly protocols, takes
about 20–30 minutes and makes about 27 OpenRouter requests. `--stdio-cmd=` skips MCP over stdio, which only works on
the VM itself.

```powershell
.venv\Scripts\python.exe scripts\smoke_test.py --base-url $env:PROTOCOL_SERVICE_URL --stdio-cmd= --allow-openrouter
```

It reads the key from `PROTOCOL_SERVICE_API_KEY` (step 3.1) and never prints it.

### 3.4 Connecting tools

- **Any HTTP client:**
  - Base URL: the site.
  - Header: `Authorization: Bearer <key>`.
  - The routes are those of [DEPLOY.md, REST API](../DEPLOY.md#4-rest-api).
  - The OpenAPI schema at `/openapi.json` needs the header too, for example to import it into Postman.
  - Server-side clients work as they are. A web page on another domain calling the API from a browser is refused by
    the service's origin check, and the service sends no CORS headers: that needs a small change, ask for it.
- **Claude Code (MCP over HTTP):**

  ```powershell
  claude mcp add --transport http protocol-service "$env:PROTOCOL_SERVICE_URL/mcp" --header "Authorization: Bearer $env:PROTOCOL_SERVICE_API_KEY"
  ```

  Claude Code stores the key in its configuration file, in plain text.
- **Other MCP clients:** Streamable HTTP at `<site>/mcp`, with the same header.

### 3.5 The demo UI and /docs (optional)

Browsers cannot send the API key, so the UI and `/docs` are not reachable through the site. Open them through an SSH
tunnel instead. This needs the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) on the laptop, signed in
with `gcloud auth login` and set to the project with `gcloud config set project <your project id>`.

```powershell
gcloud compute ssh protocol-service --zone us-central1-a --tunnel-through-iap -- -N -L 8100:127.0.0.1:8000
```

While it runs, open http://127.0.0.1:8100/ui/. Port 8100, because the laptop's own service may use 8000.

### 3.6 Uptime check and alert

In the console, open **Monitoring → Uptime checks → Create uptime check** and set:

- Protocol HTTPS, resource type URL, host name = the site (without `https://`), path `/health`, every 5 minutes.
- An alert with your email as the notification channel.

`/health` answers without the key, so the check needs none.

## Phase 4: Speed and tuning

- **Read the times** of the convert stage from step 3.2 or the smoke test summary. A batch process uses 4 threads and
  keeps about 3.5–4 vCPUs busy, so 8 vCPU fit 2 at a time (`CONVERT_WORKERS=2`). A third would only compete for the
  same CPUs.
- **Memory:** watch it during a conversion with `docker stats --no-stream`. The limit (13 GB in
  `deploy/gcp/docker-compose.gcp.yml`) covers 2 batch processes of about 5 GB each.
- **A bigger machine later** (c4-standard-16, 16 vCPU and 60 GB, about twice the price): stop the VM, change its type,
  start it, then set `CONVERT_WORKERS=4` in `.env` and `mem_limit: 24g` in the override file.

  ```bash
  gcloud compute instances stop $VM --zone=$ZONE
  gcloud compute instances set-machine-type $VM --zone=$ZONE --machine-type=c4-standard-16
  gcloud compute instances start $VM --zone=$ZONE
  ```

- **More documents at once:** `WORKER_CONCURRENCY` (documents) × `CONVERT_WORKERS` (batches per document) batch
  processes run at the same time. Each needs about 4 vCPUs and 5 GB.
- **A GPU** (an NVIDIA L4 on g2-standard-8) would convert much faster, but needs a CUDA image and a full regression,
  because the outputs can change slightly. It is a separate piece of work.

## Operations

On the VM (`gcloud compute ssh ...`, then `cd ~/ingest_retrieve`):

| Task | Command |
|---|---|
| Logs | `docker compose logs -f protocol-service`, `docker compose logs caddy` |
| Update to the latest commit | `git pull`, `python3 scripts/check_build_context.py`, `docker build -t protocol-service .`, `docker compose up -d` |
| Change a setting | Edit `.env`, then `docker compose up -d` (recreates what changed) |
| Rotate the API key | Replace `PROXY_API_KEY` in `.env` (`openssl rand -hex 32`), `docker compose up -d caddy`, update every client |
| Restart | `docker compose restart` |

- **After an update:** results made by an older pipeline fingerprint show `stale: true`. Reprocessing sends to
  OpenRouter only what is not cached.
- **Stop and start the VM** (in Cloud Shell, after the 1.1 block): `gcloud compute instances stop $VM --zone=$ZONE`
  and `gcloud compute instances start $VM --zone=$ZONE`.
  - The containers start again by themselves, and a job that was running is re-queued.
  - The static IP, and with it the site name and its certificate, stays.
  - A stopped VM costs only its disks and the reserved IP.
  - For a fixed schedule, use **Compute Engine → Instance schedules** in the console.
- **Backups:** the daily snapshots of the data disk (14 days). To restore, create a disk from a snapshot and attach it in
  place of `protocol-data`. The archive backup of [DEPLOY.md](../DEPLOY.md#3-data-volume) works too.
- **Updates of the VM itself:** Ubuntu installs security updates automatically. Reboot now and then with `sudo reboot`;
  the containers restart by themselves.
- **Your own domain later:** point an A record at the static IP, put the name in `PROXY_SITE` and `ALLOWED_HOSTS`, and
  run `docker compose up -d`. Caddy gets the new certificate itself.

## Security notes

- **Open ports:** only 80 and 443 reach the VM. SSH only comes in through IAP, and the service port listens on
  127.0.0.1.
- **The API key:**
  - One key for every client. It is stored in `.env` (readable only by your user), and compose gives it to both
    containers.
  - Treat it as a password and rotate it if it leaks.
  - There are no per-client keys and no rate limits: Caddy's standard image has no rate limiter, so anyone with the key
    can upload and search as much as they like. Set a spending limit on the OpenRouter key.
- **The documents** sit on the data disk, which Google encrypts at rest by default, and in its snapshots.
- **Remaining items:** see the [pre-hosting checklist in DEPLOY.md](../DEPLOY.md#10-before-hosting-checklist).
