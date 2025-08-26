#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Optional Playwright import (for big-company/custom sites)
try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:
    HAVE_PLAYWRIGHT = False

import os, re, sqlite3, smtplib, ssl, requests, sys, pathlib
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import quote_plus
from datetime import datetime
from pathlib import Path
import yaml

# ---------------- .env loader ----------------
def load_env_from_dotenv(path=".env"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k and v and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

# Load .env right away so env vars are set
load_env_from_dotenv()

REQUIRED_ENVS = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]

def ensure_env():
    missing = [k for k in REQUIRED_ENVS if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\n\nSet them in PowerShell, e.g.:\n"
              "  $env:SMTP_HOST=\"smtp.gmail.com\"\n"
              "  $env:SMTP_PORT=\"587\"\n"
              "  $env:SMTP_USER=\"your@gmail.com\"\n"
              "  $env:SMTP_PASS=\"<16-char app password (no spaces)>\"\n"
              "  $env:MAIL_FROM=\"your@gmail.com\"\n"
              "  $env:MAIL_TO=\"you@example.com\"\n\n"
              "Or create a .env file next to job_watcher.py like:\n"
              "  SMTP_HOST=smtp.gmail.com\n"
              "  SMTP_PORT=587\n"
              "  SMTP_USER=your@gmail.com\n"
              "  SMTP_PASS=xxxxxxxxxxxxxxxx\n"
              "  MAIL_FROM=your@gmail.com\n"
              "  MAIL_TO=you@example.com\n"
        )
# ---------------------------------------------

ROOT = pathlib.Path(__file__).parent
DB_PATH = ROOT / "seen_jobs.sqlite3"
CONF_PATH = ROOT / "config.yaml"
POLL_TIMEOUT = (4, 20)

# Flags from CLI
TEST_EMAIL = "--test-email" in sys.argv
DRY_RUN = "--dry-run" in sys.argv

STATE_FILE = os.environ.get("STATE_FILE")  # e.g., "state_seen.txt"

def load_state_ids():
    if not STATE_FILE:
        return None
    p = Path(STATE_FILE)
    if not p.exists():
        return set()
    return set(x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip())

def save_state_ids(ids):
    if not STATE_FILE:
        return
    Path(STATE_FILE).write_text("\n".join(sorted(ids)), encoding="utf-8")

def load_conf():
    with open(CONF_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen (
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        url TEXT,
        first_seen_utc TEXT,
        PRIMARY KEY (source, external_id)
    )""")
    conn.commit()
    return conn

def normalize(t):
    return (t or "").lower()

def matches_keywords(conf, title, desc):
    t = normalize(title) + "\n" + normalize(desc)
    any_pats = conf.get("keywords", {}).get("any", [])
    must_not = conf.get("keywords", {}).get("must_not", [])
    if any(re.search(p, t) for p in must_not):
        return False
    return (not any_pats) or any(re.search(p, t) for p in any_pats)

def preferred_location_score(conf, location):
    loc = normalize(location or "")
    score = 0
    for i, want in enumerate(conf.get("locations_prefer", [])):
        if normalize(want) in loc:
            score += (100 - i)
    return score

# ---------- filtering helpers ----------
def title_allowed(conf, title: str) -> bool:
    t = (title or "").lower()
    f = conf.get("filters", {})
    must_inc = f.get("titles_must_include", [])
    if must_inc and not any(re.search(p, t) for p in must_inc):
        return False
    for pat in f.get("titles_must_not", []):
        if re.search(pat, t):
            return False
    return True

# .NET/C# tech gate (strict)
def tech_stack_allowed(conf, title: str, desc: str) -> bool:
    tf = conf.get("tech_filters", {})
    text = f"{(title or '').lower()}\n{(desc or '').lower()}"
    must_all = tf.get("must_all", [])
    must_any = tf.get("must_any", [])
    # all must_all patterns must be present
    for pat in must_all:
        if not re.search(pat, text):
            return False
    # at least one of must_any (if provided)
    if must_any and not any(re.search(p, text) for p in must_any):
        return False
    return True

_exp_single = re.compile(r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:years?|yrs?)\b", re.I)
_exp_range  = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\s*(?:years?|yrs?)\b", re.I)

def max_years_mentioned(text: str):
    if not text:
        return None
    mx = None
    for a, b in _exp_range.findall(text):
        hi = max(int(a), int(b))
        mx = hi if mx is None or hi > mx else mx
    for n, in _exp_single.findall(text):
        val = int(n)
        mx = val if mx is None or val > mx else mx
    return mx

def experience_allowed(conf, title: str, desc: str) -> bool:
    filt = conf.get("filters", {})
    max_ok = int(filt.get("exp_max_years", 5))
    text = f"{title or ''}\n{desc or ''}"
    # hard negatives like 6+ years, 10 years
    for pat in filt.get("exp_must_not_patterns", []):
        if re.search(pat, text, flags=re.I):
            return False
    mx = max_years_mentioned(text)
    if mx is None:
        # allow if years not explicitly stated
        return True
    return mx <= max_ok

def location_allowed(conf, location: str) -> bool:
    loc = (location or "")
    loc_l = loc.lower().strip()
    f = conf.get("filters", {})

    # Blocklist first
    for bad in f.get("locations_must_not", []):
        if bad.lower() in loc_l:
            return False

    # US state abbreviations like "Austin, TX"
    st_pat = f.get("state_abbrev_regex")
    if st_pat and re.search(st_pat, loc, flags=re.I):
        return True

    # Allowlist (Remote/US/cities/states by name)
    allow_any = [x.lower() for x in f.get("locations_allow_any", [])]
    if allow_any and any(x in loc_l for x in allow_any):
        return True

    # If no allow list configured, allow by default
    return not allow_any

def passes_all_filters(conf, job) -> bool:
    if not title_allowed(conf, job["title"]):
        return False
    if not experience_allowed(conf, job["title"], job["desc"]):
        return False
    if not location_allowed(conf, job.get("location", "")):
        return False
    if not tech_stack_allowed(conf, job["title"], job["desc"]):
        return False
    return matches_keywords(conf, job["title"], job["desc"])
# ---------------------------------------

# ---------- Fetchers (public/ToS-safe) ----------
def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{quote_plus(slug)}/jobs?content=true"
    r = requests.get(url, timeout=POLL_TIMEOUT)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield {
            "source": f"greenhouse:{slug}",
            "id": str(j.get("id")),
            "title": j.get("title") or "",
            "location": (j.get("location") or {}).get("name", "") or "",
            "desc": j.get("content") or "",
            "url": j.get("absolute_url") or "",
        }

def fetch_lever(handle):
    url = f"https://api.lever.co/v0/postings/{quote_plus(handle)}?mode=json"
    r = requests.get(url, timeout=POLL_TIMEOUT)
    r.raise_for_status()
    for j in r.json():
        jid = j.get("id") or j.get("lever_id") or j.get("hostedUrl") or j.get("applyUrl")
        yield {
            "source": f"lever:{handle}",
            "id": str(jid),
            "title": j.get("text") or "",
            "location": (j.get("categories", {}) or {}).get("location", "") or "",
            "desc": j.get("descriptionPlain") or j.get("description") or "",
            "url": j.get("hostedUrl") or j.get("applyUrl") or "",
        }

def fetch_ashby(org_slug):
    # Public non-user GraphQL endpoint that many Ashby boards expose.
    q = {
        "operationName": "FindJobs",
        "variables": {"organizationSlug": org_slug, "page": 1},
        "query": """query FindJobs($organizationSlug: String!, $page: Int) {
            jobPostings(organizationSlug:$organizationSlug, page:$page, statuses:[PUBLISHED]) {
              totalCount
              jobPostings { id title locationSlug locationName absoluteUrl descriptionText }
            }
        }"""
    }
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    r = requests.post(url, json=q, timeout=POLL_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    posts = (((data or {}).get("data") or {}).get("jobPostings") or {}).get("jobPostings") or []
    for j in posts:
        yield {
            "source": f"ashby:{org_slug}",
            "id": str(j.get("id")),
            "title": j.get("title") or "",
            "location": j.get("locationName") or j.get("locationSlug") or "",
            "desc": j.get("descriptionText") or "",
            "url": j.get("absoluteUrl") or "",
        }
# -------------------------------------------------

# ---------- Custom sites via Playwright ----------
def fetch_playwright_css(entry):
    """
    Generic CSS-scraper using Playwright.
    config.yaml entry example:

    custom_sites:
      - name: "google"
        kind: "playwright_css"
        url:  "https://careers.google.com/jobs/results/?location=United%20States&employment_type=FULL_TIME"
        item: "[data-testid='job-card']"
        title: "[data-testid='job-card-title']"
        location: "[data-testid='job-card-location']"
        link: "a[href*='/jobs/results/']"
    """
    if not HAVE_PLAYWRIGHT:
        raise RuntimeError("Playwright not installed. Run: pip install playwright && python -m playwright install --with-deps chromium")

    url = entry["url"]
    item_sel = entry["item"]; title_sel = entry["title"]; loc_sel = entry["location"]; link_sel = entry["link"]
    name = entry.get("name", "custom")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        items = page.locator(item_sel)
        count = items.count()
        for i in range(count):
            it = items.nth(i)
            try:
                title = it.locator(title_sel).inner_text(timeout=1500) if it.locator(title_sel).count() else ""
            except Exception:
                title = ""
            try:
                location = it.locator(loc_sel).inner_text(timeout=1500) if it.locator(loc_sel).count() else ""
            except Exception:
                location = ""
            link = ""
            try:
                if it.locator(link_sel).count():
                    link = it.locator(link_sel).first.get_attribute("href") or ""
            except Exception:
                link = ""

            if link and link.startswith("/"):
                # make absolute
                base = url.split("/", 3)[:3]
                link = "/".join(base) + link

            yield {
                "source": f"custom:{name}",
                "id": f"{name}:{(link or title)[:180]}",
                "title": (title or "").strip(),
                "location": (location or "").strip(),
                "desc": "",   # optional: open link and pull detail if you want richer filtering
                "url": link or url,
            }

        ctx.close()
        browser.close()
# -------------------------------------------------

def send_email(conf, items):
    # Use .get() safely; ensure_env() will guard against missing values
    host = os.environ.get("SMTP_HOST"); port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER"); pwd = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM"); mail_to = os.environ.get("MAIL_TO")

    lines = []
    for it in items:
        lines.append(
            f"{it['title']} — {it.get('location','').strip()}\n"
            f"{it['url']}\n"
            f"Source: {it['source']}\n"
        )
    body = "\n".join(lines) if items else "Test email from Job Watcher — SMTP is configured correctly."

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = (
        f"{conf['email'].get('subject_prefix','[NEW JOB]')} {len(items)} matching role(s)"
        if items else "[TEST] Job Watcher SMTP OK"
    )
    msg["From"] = formataddr((conf['email'].get('from_name','Job Watcher'), mail_from))
    msg["To"] = mail_to

    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ctx)
        s.login(user, pwd)
        s.sendmail(mail_from, [mail_to], msg.as_string())

def main():
    conf = load_conf()
    ensure_env()  # validate env before using

    if TEST_EMAIL:
        send_email(conf, [])
        print("Sent test email.")
        return

    conn = ensure_db()
    state_ids = load_state_ids()
    new_hits = []

    def consider(j):
        key = f"{j['source']}::{j['id']}"

        # File-based state (GitHub Actions)
        if state_ids is not None:
            if key in state_ids:
                return
            if passes_all_filters(conf, j):
                new_hits.append(j)
                if not DRY_RUN:
                    state_ids.add(key)
            else:
                if DRY_RUN:
                    print(f"[FILTERED] {j['title']} — {j.get('location','').strip()}  {j['url']}")
            return

        # SQLite (local)
        if not passes_all_filters(conf, j):
            if DRY_RUN:
                print(f"[FILTERED] {j['title']} — {j.get('location','').strip()}  {j['url']}")
            return
        cur = conn.execute("SELECT 1 FROM seen WHERE source=? AND external_id=?", (j["source"], j["id"]))
        if cur.fetchone() is None:
            new_hits.append(j)
            if not DRY_RUN:
                conn.execute(
                    "INSERT INTO seen(source, external_id, url, first_seen_utc) VALUES (?,?,?,?)",
                    (j["source"], j["id"], j["url"], datetime.utcnow().isoformat(timespec='seconds'))
                )
                conn.commit()

    # Poll known platforms
    for slug in conf.get("companies", {}).get("greenhouse", []):
        try:
            for j in fetch_greenhouse(slug): consider(j)
        except Exception as e:
            print(f"[warn] greenhouse {slug}: {e}", file=sys.stderr)

    for handle in conf.get("companies", {}).get("lever", []):
        try:
            for j in fetch_lever(handle): consider(j)
        except Exception as e:
            print(f"[warn] lever {handle}: {e}", file=sys.stderr)

    for org in conf.get("companies", {}).get("ashby", []):
        try:
            for j in fetch_ashby(org): consider(j)
        except Exception as e:
            print(f"[warn] ashby {org}: {e}", file=sys.stderr)

    # Poll custom sites via Playwright (Meta/Amazon/Google/MSFT etc. via public search pages)
    for entry in (conf.get("custom_sites") or []):
        try:
            kind = entry.get("kind")
            if kind == "playwright_css":
                for j in fetch_playwright_css(entry): consider(j)
            else:
                print(f"[warn] custom site kind not supported: {kind}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] custom site {entry.get('name','?')}: {e}", file=sys.stderr)

    # Sort for readability (location preference first, then title)
    new_hits.sort(key=lambda x: (-preferred_location_score(conf, x["location"]), x["title"].lower()))

    if DRY_RUN:
        if new_hits:
            print("[DRY RUN] New matches that would be emailed:\n")
            for it in new_hits:
                print(f"- {it['title']} — {it.get('location','').strip()}\n  {it['url']}\n  Source: {it['source']}\n")
        else:
            print("[DRY RUN] No new matches found.")
        return

    if new_hits:
        if state_ids is not None:
            save_state_ids(state_ids)
        send_email(conf, new_hits)
        print(f"Sent {len(new_hits)} new match(es).")
    else:
        print("No new matches.")

if __name__ == "__main__":
    main()
