#!/usr/bin/env python3
import os, re, sqlite3, smtplib, ssl, requests, sys, pathlib
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import quote_plus
from datetime import datetime
from pathlib import Path
import yaml

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
    any_pats = conf["keywords"]["any"]
    must_not = conf["keywords"].get("must_not", [])
    if any(re.search(p, t) for p in must_not):
        return False
    return any(re.search(p, t) for p in any_pats)

def preferred_location_score(conf, location):
    loc = normalize(location or "")
    score = 0
    for i, want in enumerate(conf.get("locations_prefer", [])):
        if normalize(want) in loc:
            score += (100 - i)
    return score

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

def send_email(conf, items):
    host = os.environ["SMTP_HOST"]; port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]; pwd = os.environ["SMTP_PASS"]
    mail_from = os.environ["MAIL_FROM"]; mail_to = os.environ["MAIL_TO"]

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
    if TEST_EMAIL:
        # Send a simple email to verify SMTP works
        send_email(conf, [])
        print("Sent test email.")
        return

    conn = ensure_db()
    state_ids = load_state_ids()
    new_hits = []

    def consider(j):
        key = f"{j['source']}::{j['id']}"

        # File-based state (e.g., GitHub Actions)
        if state_ids is not None:
            if key in state_ids:
                return
            if matches_keywords(conf, j["title"], j["desc"]):
                new_hits.append(j)
                if not DRY_RUN:
                    state_ids.add(key)
            return

        # SQLite (local)
        if not (matches_keywords(conf, j["title"], j["desc"])):
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

    # Poll sources
    for slug in conf["companies"].get("greenhouse", []):
        try:
            for j in fetch_greenhouse(slug): consider(j)
        except Exception as e:
            print(f"[warn] greenhouse {slug}: {e}", file=sys.stderr)

    for handle in conf["companies"].get("lever", []):
        try:
            for j in fetch_lever(handle): consider(j)
        except Exception as e:
            print(f"[warn] lever {handle}: {e}", file=sys.stderr)

    for org in conf["companies"].get("ashby", []):
        try:
            for j in fetch_ashby(org): consider(j)
        except Exception as e:
            print(f"[warn] ashby {org}: {e}", file=sys.stderr)

    # Sort for readability
    new_hits.sort(key=lambda x: (-preferred_location_score(conf, x["location"]), x["title"].lower()))

    if DRY_RUN:
        # Print what would be sent, without touching state or email
        if new_hits:
            print("[DRY RUN] New matches that would be emailed:\n")
            for it in new_hits:
                print(f"- {it['title']} — {it.get('location','').strip()}\n  {it['url']}\n  Source: {it['source']}\n")
        else:
            print("[DRY RUN] No new matches found.")
        return

    if new_hits:
        # persist STATE_FILE if used
        if state_ids is not None:
            save_state_ids(state_ids)
        send_email(conf, new_hits)
        print(f"Sent {len(new_hits)} new match(es).")
    else:
        print("No new matches.")

if __name__ == "__main__":
    main()
