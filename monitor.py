"""Lucid Grant Monitor — single-file grant & procurement watcher.

Polls SAM.gov, the EU Funding & Tenders Portal, Grants.gov, UKRI, DARPA,
DIU, and ARIA on a schedule, scores every opportunity against a
keyword list, and posts anything
relevant to Slack. Deduplicates via a local SQLite DB so the same opportunity
is never posted twice.

Usage:
    python monitor.py                  # run forever (scheduler loop)
    python monitor.py --dry-run        # scrape + score + print, no DB, no Slack
    python monitor.py --dry-run --source sam_gov
    python monitor.py --send-digest    # send the daily digest right now

Config comes from .env — see .env.example and README.md.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "#grants")
SAM_GOV_API_KEY = os.environ.get("SAM_GOV_API_KEY", "")

SCORE_IMMEDIATE_THRESHOLD = int(os.environ.get("SCORE_IMMEDIATE_THRESHOLD", "15"))
SCORE_DIGEST_THRESHOLD = int(os.environ.get("SCORE_DIGEST_THRESHOLD", "5"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "8"))
TZ = os.environ.get("TZ", "Europe/Stockholm")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "seen.db")

DRY_RUN = False  # set by --dry-run in main()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("monitor")

# --------------------------------------------------------------------------
# Keywords & scoring
# --------------------------------------------------------------------------

KEYWORDS = {
    10: [
        "AI governance", "AI safety", "model evaluation", "model audit",
        "trusted execution environment", "TEE", "confidential computing",
        "hardware attestation", "chip attestation", "AI chip",
        "compute governance", "compute metering", "training compute",
        "zero-knowledge proof", "ZK proof", "AI verification",
        "datacenter attestation", "datacenter verification",
        "sovereign compute", "sovereign AI",
    ],
    5: [
        "digital sovereignty", "AI infrastructure", "AI datacenter",
        "neocloud", "export control", "investment screening", "AI treaty",
        "AI arms control", "secure enclave", "AI red teaming", "AI security",
        "AI integrity", "government AI", "AI procurement",
        "supply chain security", "hardware security", "frontier model",
        "foundation model", "AI regulation", "AI Act",
    ],
    2: [
        "artificial intelligence", "machine learning", "deep learning",
        "cloud security", "digital infrastructure", "AI capability",
        "AI risk", "cybersecurity", "cryptography", "data center",
        "semiconductor",
    ],
}

# Pre-compile one word-bounded, case-insensitive pattern per keyword.
_PATTERNS = [
    (points, kw, re.compile(r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])", re.IGNORECASE))
    for points, kws in KEYWORDS.items()
    for kw in kws
]


def score_item(title, description):
    """Return (score, matched_keywords). A match in the title counts double."""
    total = 0
    matched = []
    for points, kw, pattern in _PATTERNS:
        in_title = bool(pattern.search(title or ""))
        in_desc = bool(pattern.search(description or ""))
        if not in_title and not in_desc:
            continue
        total += points * 2 if in_title else points
        matched.append(kw)
    return total, matched


# --------------------------------------------------------------------------
# Database (SQLite deduplication)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
  id TEXT PRIMARY KEY,        -- "{source}:{opportunity_id}"
  title TEXT,
  score INTEGER,
  notified INTEGER DEFAULT 0, -- 1 = sent immediate alert
  in_digest INTEGER DEFAULT 0,-- 1 = included in a digest
  found_at TEXT DEFAULT CURRENT_TIMESTAMP,
  source TEXT,
  deadline TEXT,
  url TEXT
);
CREATE TABLE IF NOT EXISTS digest_log (
  digest_date TEXT PRIMARY KEY,   -- "YYYY-MM-DD" of the digest cycle
  sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
  item_count INTEGER              -- how many items the digest contained (0 = empty)
);
"""

_db = None


def get_db():
    """Open the DB. In dry-run mode, open read-only (or not at all if absent)."""
    global _db
    if _db is not None:
        return _db
    if DRY_RUN:
        if not os.path.exists(DB_PATH):
            return None
        _db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        return _db
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _db = sqlite3.connect(DB_PATH)
    _db.executescript(SCHEMA)
    _db.commit()
    return _db


def digest_sent_today(db):
    """True if a digest cycle has already been recorded for today."""
    if db is None:
        return False
    today = datetime.now().date().isoformat()
    try:
        row = db.execute(
            "SELECT 1 FROM digest_log WHERE digest_date = ?", (today,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def record_digest_sent(db, item_count):
    """Mark today's digest cycle as done (idempotent)."""
    if DRY_RUN or db is None:
        return
    today = datetime.now().date().isoformat()
    db.execute(
        "INSERT OR REPLACE INTO digest_log (digest_date, item_count) VALUES (?, ?)",
        (today, item_count),
    )
    db.commit()


def already_seen(uid):
    db = get_db()
    if db is None:
        return False
    try:
        row = db.execute("SELECT 1 FROM seen WHERE id = ?", (uid,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def record_item(uid, item, score, notified):
    if DRY_RUN:
        return
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO seen (id, title, score, notified, source, deadline, url)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, item["title"], score, notified, item["source"],
         item.get("deadline"), item.get("url")),
    )
    db.commit()


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

_slack = None


def post_to_slack(text):
    if DRY_RUN:
        log.info("[dry-run] would post to Slack:\n%s", text)
        return
    global _slack
    if _slack is None:
        _slack = WebClient(token=SLACK_BOT_TOKEN)
    try:
        _slack.chat_postMessage(channel=SLACK_CHANNEL, text=text, unfurl_links=False)
    except SlackApiError as e:
        log.error("Slack post failed: %s", e.response.get("error", e))


def post_immediate_alert(item, matched):
    keywords = " ".join(f"`{kw}`" for kw in matched[:6])
    post_to_slack(
        f"🔔 *New opportunity — {item['source']}*\n"
        f"*{item['title']}*\n"
        f"📅 Deadline: {item.get('deadline') or 'not specified'}\n"
        f"💰 Budget: {item.get('budget') or 'not specified'}\n"
        f"🏷️ Matched: {keywords}\n"
        f"🔗 {item.get('url') or ''}"
    )


# Operational warnings (e.g. a missing/expired API key) are throttled per key
# so a persistent problem doesn't spam the channel on every scheduled run.
WARN_THROTTLE_SECONDS = 12 * 3600
_last_warned = {}


def post_warning(key, text):
    """Post a throttled operational warning to Slack (at most once per key
    per WARN_THROTTLE_SECONDS)."""
    now = time.monotonic()
    last = _last_warned.get(key)
    if last is not None and now - last < WARN_THROTTLE_SECONDS:
        return
    _last_warned[key] = now
    post_to_slack(f"⚠️ {text}")


def send_digest(force=False):
    """Send the daily digest.

    The automated callers (scheduled job and the startup catch-up) pass
    force=False: they skip if a digest cycle is already recorded for today,
    and record one when done — even an empty one, so the cycle runs at most
    once per day. The manual --send-digest flag passes force=True to bypass
    that bookkeeping and always send.
    """
    db = get_db()
    if db is None:
        log.info("Digest: no database yet, nothing to send")
        return
    if not force and digest_sent_today(db):
        log.info("Digest: already sent today, skipping")
        return
    rows = db.execute(
        "SELECT id, title, source, deadline, score, url FROM seen"
        " WHERE in_digest = 0 AND score >= ? ORDER BY score DESC",
        (SCORE_DIGEST_THRESHOLD,),
    ).fetchall()
    if not rows:
        log.info("Digest: nothing above threshold today, posting heartbeat")
        post_to_slack(
            f"📋 *Daily grant digest — {datetime.now().date().isoformat()}*\n"
            f"No new opportunities above threshold (score ≥ {SCORE_DIGEST_THRESHOLD}) "
            "since yesterday. Monitor is running. ✅"
        )
        if not force:
            record_digest_sent(db, 0)
        return
    lines = [
        f"📋 *Daily grant digest — {datetime.now().date().isoformat()}*",
        f"{len(rows)} new opportunities since yesterday (score ≥ {SCORE_DIGEST_THRESHOLD})",
        "",
    ]
    for _, title, source, deadline, score, url in rows:
        lines.append(
            f"• *{title}* | {source} | deadline {deadline or 'not specified'}"
            f" | score {score} | {url or ''}"
        )
    post_to_slack("\n".join(lines))
    if not DRY_RUN:
        db.executemany("UPDATE seen SET in_digest = 1 WHERE id = ?",
                       [(row[0],) for row in rows])
        db.commit()
    if not force:
        record_digest_sent(db, len(rows))
    log.info("Digest sent with %d items", len(rows))


# --------------------------------------------------------------------------
# HTTP helper (retries with exponential backoff)
# --------------------------------------------------------------------------

def http_request(method, url, retries=3, **kwargs):
    """GET/POST with exponential backoff. Pass retries=1 to make a single
    request with no retries — used for non-critical, quota-limited calls."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("headers", {"User-Agent": "lucid-grant-monitor/1.0"})
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_error = e
            if attempt < retries - 1:
                wait = 2 ** attempt
                log.warning("HTTP error on %s (attempt %d/%d): %s — retrying in %ds",
                            url, attempt + 1, retries, e, wait)
                time.sleep(wait)
    raise last_error


def strip_html(html):
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


# --------------------------------------------------------------------------
# Scrapers — each returns a list of dicts with keys:
#   id, title, description, deadline, budget, url, source
# --------------------------------------------------------------------------

# Max SAM.gov description fetches per run (1 request each). With the 12-hour
# cadence (2 runs/day) the daily budget is 2*(1 + this) requests — keep it at
# 4 to stay within the ~10/day personal-key quota.
SAM_GOV_MAX_DESCRIPTIONS = 4


def scrape_sam_gov():
    """SAM.gov Opportunities API v2. Free API key required."""
    if not SAM_GOV_API_KEY:
        log.warning("SAM.gov: no SAM_GOV_API_KEY set, skipping")
        post_warning("sam_gov_key",
                     "*SAM.gov API key missing.* `SAM_GOV_API_KEY` is not set, "
                     "so SAM.gov monitoring is paused. Add a free key from "
                     "https://sam.gov/profile to your `.env` and restart.")
        return []
    now = datetime.now()
    try:
        resp = http_request("GET", "https://api.sam.gov/opportunities/v2/search", params={
            "api_key": SAM_GOV_API_KEY,
            "postedFrom": (now - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y"),
            "postedTo": now.strftime("%m/%d/%Y"),
            "limit": 100,
        })
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            log.error("SAM.gov: API key rejected (HTTP %s)", status)
            post_warning("sam_gov_key",
                         f"*SAM.gov API key rejected* (HTTP {status}) — it is "
                         "likely expired or invalid, so SAM.gov monitoring is "
                         "paused. Update `SAM_GOV_API_KEY` in your `.env` "
                         "(regenerate at https://sam.gov/profile) and restart.")
            return []
        raise
    # The personal SAM.gov key is capped at ~10 requests/day. At the 12-hour
    # cadence that's 2 runs/day; budgeting 1 search + up to SAM_GOV_MAX_DESCRIPTIONS
    # description fetches per run keeps the worst case at 2*(1+4) = 10/day.
    # The v2 search exposes the description only as a URL, so we fetch it —
    # single-shot (retries=1) so each fetch costs exactly one request — but
    # only for unseen notices whose title alone already matches a keyword, and
    # only up to the per-run cap. That way quota is spent confirming/enriching
    # already-promising notices, not on titles that look irrelevant anyway.
    results = []
    desc_fetches = 0
    for item in resp.json().get("opportunitiesData", []):
        notice_id = item.get("noticeId")
        if not notice_id:
            continue
        title = item.get("title", "")
        description = ""
        desc_url = item.get("description") or ""
        if (desc_url.startswith("http")
                and desc_fetches < SAM_GOV_MAX_DESCRIPTIONS
                and not already_seen(f"SAM.gov:{notice_id}")
                and score_item(title, "")[0] > 0):
            desc_fetches += 1
            try:
                desc_resp = http_request("GET", desc_url, retries=1,
                                         params={"api_key": SAM_GOV_API_KEY})
                description = strip_html(desc_resp.json().get("description", ""))
            except Exception as e:
                log.warning("SAM.gov: description fetch failed for %s: %s",
                            notice_id, e)
        award = item.get("award") or {}
        results.append({
            "id": notice_id,
            "title": title,
            "description": description,
            "deadline": item.get("responseDeadLine"),
            "budget": award.get("amount"),
            "url": item.get("uiLink")
                   or f"https://sam.gov/opp/{notice_id}/view",
            "source": "SAM.gov",
        })
    return results


def scrape_eu_portal():
    """EU Funding & Tenders Portal (SEDIA) search API. No key needed."""
    query = {"bool": {"must": [
        {"terms": {"type": ["1", "2"]}},                  # calls & topics
        {"terms": {"status": ["31094501", "31094502"]}},  # forthcoming, open
    ]}}
    resp = http_request(
        "POST", "https://api.tech.ec.europa.eu/search-api/prod/rest/search",
        params={"apiKey": "SEDIA", "text": "***", "pageSize": 100, "pageNumber": 1},
        # The SEDIA API requires explicit application/json content types on
        # the multipart parts; bare (None, value) parts get rejected with 500.
        files={
            "query": ("query.json", json.dumps(query), "application/json"),
            "languages": ("languages.json", '["en"]', "application/json"),
            "sort": ("sort.json", '{"field":"startDate","order":"DESC"}', "application/json"),
        },
    )
    results = []
    for hit in resp.json().get("results", []):
        meta = hit.get("metadata") or {}

        def first(key):
            values = meta.get(key) or []
            return values[0] if values else None

        identifier = first("identifier") or hit.get("reference")
        if not identifier:
            continue
        results.append({
            "id": identifier,
            "title": strip_html(first("title") or ""),
            "description": strip_html(hit.get("summary")
                                      or first("descriptionByte") or ""),
            "deadline": (first("deadlineDate") or "")[:10] or None,
            "budget": None,
            "url": ("https://ec.europa.eu/info/funding-tenders/opportunities/"
                    f"portal/screen/opportunities/topic-details/{identifier.lower()}"),
            "source": "EU Portal",
        })
    return results


def scrape_grants_gov():
    """Grants.gov public search REST API. No key needed."""
    resp = http_request(
        "POST", "https://apply07.grants.gov/grantsws/rest/opportunities/search/",
        json={"keyword": "", "oppStatuses": "posted", "sortBy": "openDate|desc",
              "rows": 100, "startRecordNum": 0},
    )
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    results = []
    for hit in resp.json().get("oppHits", []):
        opp_id = hit.get("id")
        if not opp_id:
            continue
        # Keep only the lookback window (openDate is MM/DD/YYYY).
        try:
            if datetime.strptime(hit.get("openDate", ""), "%m/%d/%Y") < cutoff:
                continue
        except ValueError:
            pass
        results.append({
            "id": str(opp_id),
            "title": hit.get("title", ""),
            "description": hit.get("agencyName") or hit.get("agency") or "",
            "deadline": hit.get("closeDate"),
            "budget": None,
            "url": f"https://www.grants.gov/search-results-detail/{opp_id}",
            "source": "Grants.gov",
        })
    return results


def scrape_ukri():
    """UKRI / Innovate UK funding opportunity RSS feed."""
    resp = http_request("GET", "https://www.ukri.org/opportunity/feed/")
    root = ET.fromstring(resp.content)
    cutoff = datetime.now().astimezone() - timedelta(days=max(LOOKBACK_DAYS, 7))
    results = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published < cutoff:
                continue
        except (TypeError, ValueError):
            pass
        results.append({
            "id": link.rstrip("/").split("/")[-1],
            "title": (item.findtext("title") or "").strip(),
            "description": strip_html(item.findtext("description") or ""),
            "deadline": None,
            "budget": None,
            "url": link,
            "source": "UKRI",
        })
    return results


def scrape_darpa():
    """DARPA opportunities RSS feed."""
    resp = http_request("GET", "https://www.darpa.mil/rss/opportunities.xml")
    root = ET.fromstring(resp.content)
    cutoff = datetime.now().astimezone() - timedelta(days=max(LOOKBACK_DAYS, 7))
    results = []
    for item in root.iter("item"):
        # Every item's <link> points at the same opportunities page, so the
        # unique id comes from <guid> (format: "5011 at https://www.darpa.mil").
        guid = (item.findtext("guid") or "").strip().split(" ")[0]
        if not guid:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published < cutoff:
                continue
        except (TypeError, ValueError):
            pass
        results.append({
            "id": guid,
            "title": (item.findtext("title") or "").strip(),
            "description": strip_html(item.findtext("description") or ""),
            "deadline": None,
            "budget": None,
            "url": (item.findtext("link") or "").strip(),
            "source": "DARPA",
        })
    return results


def scrape_diu():
    """DIU (Defense Innovation Unit) open solicitations. No RSS feed exists,
    so this scrapes the page HTML: each solicitation is a div.aoi card with
    the title in .title h4 and a /work-with-us/submit-solution/PROJ… link."""
    try:
        resp = http_request("GET", "https://www.diu.mil/work-with-us/open-solicitations")
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 403:
            log.warning("DIU: page returned 403, skipping this run")
            return []
        raise
    soup = BeautifulSoup(resp.content, "html.parser")
    results = []
    for card in soup.select("div.aoi"):
        anchor = card.find("a", href=lambda h: h and (
            "/work-with-us/" in h or "/solicitations/" in h))
        title = card.select_one(".title h4")
        if not anchor or not title:
            continue
        href = anchor["href"]
        url = href if href.startswith("http") else f"https://www.diu.mil{href}"
        deadline = None
        closing = card.select_one(".closing-date")
        if closing:
            match = re.search(r"\d{4}-\d{2}-\d{2}", closing.get_text())
            deadline = match.group(0) if match else None
        results.append({
            "id": url.rstrip("/").split("/")[-1],
            "title": title.get_text(" ", strip=True),
            "description": card.get_text(" ", strip=True)[:2000],
            "deadline": deadline,
            "budget": None,
            "url": url,
            "source": "DIU",
        })
    return results


def scrape_aria():
    """ARIA (UK Advanced Research + Invention Agency) funding opportunities.
    No RSS feed; scrapes the open-call cards on the funding page (the old
    /programmes/ path now 404s). ARIA posts rarely, so 0 results is normal."""
    try:
        resp = http_request("GET", "https://aria.org.uk/funding-opportunities")
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 403:
            log.warning("ARIA: page returned 403, skipping this run")
            return []
        raise
    soup = BeautifulSoup(resp.content, "html.parser")
    results = []
    seen_hrefs = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/opportunity-spaces/" not in href and "/funding-opportunities/" not in href:
            continue
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        # The link text is just "Learn more about this call" — the title is
        # the heading of the enclosing card, so walk up until one appears.
        title = None
        node = anchor
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            heading = node.find(["h1", "h2", "h3", "h4"])
            if heading:
                title = heading.get_text(" ", strip=True)
                break
        if not title:
            continue
        url = href if href.startswith("http") else f"https://aria.org.uk{href}"
        results.append({
            "id": url.rstrip("/").split("/")[-1],
            "title": title,
            "description": node.get_text(" ", strip=True)[:2000],
            "deadline": None,
            "budget": None,
            "url": url,
            "source": "ARIA",
        })
    return results


SCRAPERS = {
    "sam_gov": scrape_sam_gov,
    "eu_portal": scrape_eu_portal,
    "grants_gov": scrape_grants_gov,
    "ukri": scrape_ukri,
    "darpa": scrape_darpa,
    "diu": scrape_diu,
    "aria": scrape_aria,
}


# --------------------------------------------------------------------------
# Core run loop
# --------------------------------------------------------------------------

def run_scraper(scraper):
    """Fetch, score, dedupe, alert. Never lets one source crash the process."""
    name = scraper.__name__
    log.info("Running %s ...", name)
    try:
        items = scraper()
    except Exception as e:
        log.error("%s failed: %s", name, e)
        return
    new_count = alert_count = 0
    for item in items:
        uid = f"{item['source']}:{item['id']}"
        if already_seen(uid):
            continue
        new_count += 1
        score, matched = score_item(item["title"], item.get("description", ""))
        notified = 0
        if score >= SCORE_IMMEDIATE_THRESHOLD:
            post_immediate_alert(item, matched)
            notified = 1
            alert_count += 1
        elif DRY_RUN and score >= SCORE_DIGEST_THRESHOLD:
            log.info("[dry-run] digest candidate (score %d, %s): %s",
                     score, ", ".join(matched), item["title"])
        record_item(uid, item, score, notified)
    log.info("%s: %d fetched, %d new, %d immediate alerts",
             name, len(items), new_count, alert_count)


def main():
    parser = argparse.ArgumentParser(description="Lucid Grant Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape + score + print; no DB writes, no Slack")
    parser.add_argument("--source", choices=sorted(SCRAPERS),
                        help="run only this source (with --dry-run)")
    parser.add_argument("--send-digest", action="store_true",
                        help="send the daily digest now and exit")
    args = parser.parse_args()

    global DRY_RUN
    DRY_RUN = args.dry_run

    os.environ["TZ"] = TZ
    if hasattr(time, "tzset"):
        time.tzset()

    if not DRY_RUN and not SLACK_BOT_TOKEN:
        log.error("SLACK_BOT_TOKEN is not set — copy .env.example to .env "
                  "and fill it in, or use --dry-run")
        sys.exit(1)

    if args.send_digest:
        send_digest(force=True)
        return

    if args.source or DRY_RUN:
        targets = [SCRAPERS[args.source]] if args.source else list(SCRAPERS.values())
        for scraper in targets:
            run_scraper(scraper)
        return

    # Normal mode: run everything once at startup, then follow the schedule.
    for scraper in SCRAPERS.values():
        run_scraper(scraper)

    # Catch-up: if we started after DIGEST_HOUR and today's digest never ran
    # (e.g. the process was down at the scheduled time), send it now. The
    # scheduled .at() job below only fires at the *next* DIGEST_HOUR.
    if datetime.now().hour >= DIGEST_HOUR and not digest_sent_today(get_db()):
        log.info("Catch-up: today's digest has not run yet, sending now")
        send_digest()

    schedule.every(12).hours.do(run_scraper, scrape_sam_gov)
    schedule.every(12).hours.do(run_scraper, scrape_eu_portal)
    schedule.every(12).hours.do(run_scraper, scrape_grants_gov)
    schedule.every().day.do(run_scraper, scrape_ukri)
    schedule.every().day.do(run_scraper, scrape_darpa)
    schedule.every().day.do(run_scraper, scrape_diu)
    schedule.every().day.do(run_scraper, scrape_aria)
    schedule.every().day.at(f"{DIGEST_HOUR:02d}:00").do(send_digest)

    log.info("Scheduler running — digest daily at %02d:00 %s", DIGEST_HOUR, TZ)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
