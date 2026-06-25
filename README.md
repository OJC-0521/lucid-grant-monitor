# Lucid Grant Monitor

Watches US, EU, and UK funding portals for funding calls and procurement opportunities relevant to AI infrastructure, confidential computing, and datacenter governance. Posts to Slack when something relevant appears.

---

## How it works

A single Python script (`monitor.py`) runs in a loop forever. Every few hours it hits each funding portal's API, scores results against a keyword list, and posts anything above the threshold to your Slack channel. At 08:00 CET every day it sends a digest of everything new since yesterday.

Results are stored in a local SQLite file (`data/seen.db`) so the same opportunity is never posted twice, even if you restart.

### Sources

| Source | Method | Frequency |
|---|---|---|
| SAM.gov | Opportunities API v2 (key required) | Every 12h |
| EU Funding & Tenders Portal | Search API | Every 12h |
| Grants.gov | REST API | Every 12h |
| UKRI / Innovate UK | RSS feed | Daily |
| DARPA | RSS feed | Daily |
| DIU (Defense Innovation Unit) | HTML scrape of open solicitations | Daily |
| ARIA (UK) | HTML scrape of funding opportunities (posts rarely; 0 results is normal) | Daily |

---

## Prerequisites

You need one of:
- **Docker** (recommended — works identically on Mac, Linux, Windows, and any server)
- **Python 3.11+** if you'd rather run it directly

And:
- A Slack bot token (5 minutes to set up — see below)
- A SAM.gov API key (free, 2 minutes — see below)

---

## Setup

### 1. Get the code

```bash
git clone https://github.com/your-org/lucid-grant-monitor.git
cd lucid-grant-monitor
```

### 2. Create your config file

```bash
cp .env.example .env
```

Open `opin any text editor and fill in the two required values:

```
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_CHANNEL=#grants
SAM_GOV_API_KEY=your-key-here
```

Everything else has sensible defaults. Full reference at the bottom of this file.

### 3. Run it

**With Docker (recommended):**
```bash
docker-compose up -d
```

That's it. The monitor is now running in the background. Check it's alive:
```bash
docker-compose logs -f
```

**Without Docker:**
```bash
pip install -r requirements.txt
python monitor.py
```

To keep it running after you close the terminal, see the "Running on a server" section below.

---

## Getting a Slack bot token

1. Go to https://api.slack.com/apps and click **Create New App → From scratch**
2. Name it `Grant Monitor`, pick your workspace
3. In the left sidebar: **OAuth & Permissions**
4. Scroll to **Scopes → Bot Token Scopes**, add:
   - `chat:write`
   - `chat:write.public`
5. Scroll up, click **Install to Workspace**, approve it
6. Copy the **Bot OAuth Token** — it starts with `xoxb-`
7. Invite the bot to your channel: in Slack, open the channel, type `/invite @Grant Monitor`

---

## Getting a SAM.gov API key

1. Go to https://sam.gov and create a free account
2. After logging in: click your name top-right → **Profile**
3. Under **API keys**, click **Generate new key**
4. Copy it into your `.env` file

The key is free. A personal (non-federal) key is capped at roughly **10 requests/day**, so the monitor polls SAM.gov every 12 hours (2 runs/day) and budgets each run at one search plus up to four opportunity-description fetches — at most 10 requests/day, right at the limit.

---

## Deployment options

### Option A — Your laptop (simplest)

Works fine as long as your laptop is on. Docker Desktop keeps the container running in the background. If you restart your Mac, run `docker-compose up -d` again, or enable Docker Desktop's "Start on login" setting.

**Limitation:** pauses when your laptop is asleep or off.

### Option B — A cheap cloud server (recommended for reliability)

Any small VPS works. Cheapest options:

| Provider | Size | Cost | Notes |
|---|---|---|---|
| Hetzner Cloud | CX11 (1 vCPU, 2 GB RAM) | ~€4/mo | Best value in Europe |
| DigitalOcean | Basic Droplet (1 vCPU, 1 GB RAM) | $6/mo | Easy UI |
| Fly.io | shared-cpu-1x | Free tier available | Good for Docker |
| Railway | Starter | $5/mo | Easiest deploy |

**Hetzner example (Ubuntu 24.04):**

```bash
# On the server, after SSH in:
apt update && apt install -y docker.io docker-compose git
git clone https://github.com/your-org/lucid-grant-monitor.git
cd lucid-grant-monitor
cp .env.example .env
nano .env          # fill in your tokens
docker-compose up -d
```

Done. It will restart automatically if the server reboots (`restart: always` in docker-compose.yml).

Check logs anytime:
```bash
docker-compose logs -f --tail=50
```

### Option C — Run directly on a Linux server (no Docker)

```bash
# Install Python 3.11
apt install -y python3.11 python3.11-venv

# Set up the project
git clone https://github.com/your-org/lucid-grant-monitor.git
cd lucid-grant-monitor
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env

# Create a systemd service so it runs forever and restarts on reboot
sudo tee /etc/systemd/system/grant-monitor.service << 'SERVICE'
[Unit]
Description=Lucid Grant Monitor
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/lucid-grant-monitor
ExecStart=/home/ubuntu/lucid-grant-monitor/venv/bin/python monitor.py
Restart=always
RestartSec=30
EnvironmentFile=/home/ubuntu/lucid-grant-monitor/.env

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable grant-monitor
sudo systemctl start grant-monitor

# Check it's running
sudo systemctl status grant-monitor
journalctl -u grant-monitor -f
```

---

## Testing before going live

Run a dry run to see what would be posted without actually sending anything:

```bash
python monitor.py --dry-run
```

This scrapes all sources, scores everything, and prints what would go to Slack. No DB writes, no messages sent.

Test a single source:
```bash
python monitor.py --dry-run --source sam_gov
python monitor.py --dry-run --source eu_portal
```

Force the daily digest right now (useful to verify Slack is connected):
```bash
python monitor.py --send-digest
```

---

## Customising keywords

Open `monitor.py` and find the `KEYWORDS` dictionary near the top. Add or remove terms freely — no other code needs to change.

```python
KEYWORDS = {
    10: ["AI governance", "TEE", "confidential computing", ...],
     5: ["digital sovereignty", "export control", ...],
     2: ["artificial intelligence", "data center", ...],
}
```

Scoring threshold defaults (adjustable in `.env`):
- `SCORE_IMMEDIATE_THRESHOLD=15` — post to Slack immediately
- `SCORE_DIGEST_THRESHOLD=5` — include in daily digest
- Lower both if you're getting too little. Raise if too noisy.

---

## Adding a new funding source

In `monitor.py`, find the `SCRAPERS` list at the bottom. Each scraper is a function that returns a list of dicts:

```python
def scrape_my_new_source():
    resp = requests.get("https://example.com/api/opportunities")
    results = []
    for item in resp.json()["items"]:
        results.append({
            "id": item["id"],
            "title": item["title"],
            "description": item.get("description", ""),
            "deadline": item.get("closeDate"),
            "budget": item.get("awardCeiling"),
            "url": f"https://example.com/opportunity/{item['id']}",
            "source": "My New Source",
        })
    return results
```

Then add it to the schedule:
```python
schedule.every(12).hours.do(run_scraper, scrape_my_new_source)
```

---

## Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | — | Bot token from api.slack.com, starts with `xoxb-` |
| `SLACK_CHANNEL` | ✅ | — | Channel name, e.g. `#grants` |
| `SAM_GOV_API_KEY` | ✅ | — | Free key from sam.gov |
| `SCORE_IMMEDIATE_THRESHOLD` | | `15` | Min score to trigger instant Slack alert |
| `SCORE_DIGEST_THRESHOLD` | | `5` | Min score to appear in daily digest |
| `LOOKBACK_DAYS` | | `2` | How many days back each scrape looks |
| `DIGEST_HOUR` | | `8` | Hour (24h) to send daily digest, in `TZ` timezone |
| `TZ` | | `Europe/Stockholm` | Timezone for digest scheduling |

---

## Troubleshooting

**No messages appearing in Slack**
- Check the bot is invited to the channel: `/invite @Grant Monitor` in Slack
- Run `python monitor.py --dry-run` and check the output — does it find any opportunities above threshold?
- Check `docker-compose logs` for errors

**SAM.gov returning 403**
- Your API key may not be activated yet (takes ~30 min after registration)
- Check you copied the full key with no trailing spaces

**Container keeps restarting**
- `docker-compose logs` will show the Python traceback
- Most likely a missing or malformed `.env` value

**Want to see what's in the database**
```bash
sqlite3 data/seen.db "SELECT title, score, found_at FROM seen ORDER BY found_at DESC LIMIT 20;"
```
