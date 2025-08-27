#!/usr/bin/env python3
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
              "  $env:MAIL_TO=\"ramanagajula001@gmail.com\"\n\n"
              "Or create a .env file next to job_watcher.py like:\n"
              "  SMTP_HOST=smtp.gmail.com\n"
              "  SMTP_PORT=587\n"
              "  SMTP_USER=your@gmail.com\n"
              "  SMTP_PASS=xxxxxxxxxxxxxxxx\n"
              "  MAIL_FROM=your@gmail.com\n"
              "  MAIL_TO=ramanagajula001@gmail.com\n"
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
    any_pats = conf["keywords"]["any"]
    must_not = conf["keywords"].get("must_not", [])
    if any(re.search(p, t, flags=re.I) for p in must_not):
        return False
    return any(re.search(p, t, flags=re.I) for p in any_pats)

# ---------- filtering helpers ----------
def _any_regex_match(patterns, text):
    return any(re.search(p, text, flags=re.I) for p in patterns or [])

def _all_regex_match(patterns, text):
    return all(re.search(p, text, flags=re.I) for p in patterns or [])

def title_includes(conf, title: str) -> bool:
    """Honor titles_must_include (any) and optional titles_must_include_all (all)."""
    t = title or ""
    flt = conf.get("filters", {})
    must_any = flt.get("titles_must_include", [])
    must_all = flt.get("titles_must_include_all", [])
    if must_any and not _any_regex_match(must_any, t):
        return False
    if must_all and not _all_regex_match(must_all, t):
        return False
    return True

def title_allowed(conf, title: str) -> bool:
    t = (title or "").lower()
    for pat in conf.get("filters", {}).get("titles_must_not", []):
        if re.search(pat, t, flags=re.I):
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
    for pat in filt.get("exp_must_not_patterns", []):
        if re.search(pat, text, flags=re.I):
            return False
    mx = max_years_mentioned(text)
    if mx is None:
        return True
    return mx <= max_ok

def _normalize_location_tokens(loc: str) -> str:
    if not loc:
        return ""
    loc = loc.lower()
    loc = loc.replace("united states", "us").replace("u.s.", "us").replace("usa", "us")
    loc = loc.replace("remote (us)", "remote us").replace("us-remote", "remote us")
    return loc

def location_allowed(conf, location: str) -> bool:
    loc = _normalize_location_tokens((location or "").strip().lower())
    filt = conf.get("filters", {})
    for bad in filt.get("locations_must_not", []):
        if bad.lower() in loc:
            return False
    allow_any = filt.get("locations_allow_any", [])
    if not allow_any:
        return True
    return any(a.lower() in loc for a in allow_any)

def passes_all_filters(conf, job) -> bool:
    title = job["title"]
    desc = job["desc"]
    location = job.get("location", "")

    if not title_allowed(conf, title):
        return False
    if not title_includes(conf, title):
        return False
    if not experience_allowed(conf, title, desc):
        return False
    if not location_allowed(conf, location):
        return False
    return matches_keywords(conf, title, desc)
# ---------------------------------------

# ---------- Fetchers (public/ToS-safe) ----------
def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{quote_plus(slug)}/jobs?content=true"
    r = requests.get(url, timeout=POLL_TIMEOUT)
    r.raise_for_status()
    data = r.json() or {}
    for j in data.get("jobs", []):
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
    arr = r.json() or []
    for j in arr:
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
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    page = 1
    while True:
        q = {
            "operationName": "FindJobs",
            "variables": {"organizationSlug": org_slug, "page": page},
            "query": """query FindJobs($organizationSlug: String!, $page: Int) {
                jobPostings(organizationSlug:$organizationSlug, page:$page, statuses:[PUBLISHED]) {
                  totalCount
                  jobPostings { id title locationSlug locationName absoluteUrl descriptionText }
                }
            }"""
        }
        r = requests.post(url, json=q, timeout=POLL_TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
        posts = (((data.get("data") or {}).get("jobPostings") or {}).get("jobPostings") or [])
        if not posts:
            break
        for j in posts:
            yield {
                "source": f"ashby:{org_slug}",
                "id": str(j.get("id")),
                "title": j.get("title") or "",
                "location": j.get("locationName") or j.get("locationSlug") or "",
                "desc": j.get("descriptionText") or "",
                "url": j.get("absoluteUrl") or "",
            }
        page += 1

def fetch_smartrecruiters(company_slug):
    # Not all companies are on SR; safe to try.
    base = f"https://api.smartrecruiters.com/v1/companies/{quote_plus(company_slug)}/postings"
    page = 0
    while True:
        r = requests.get(base, params={"offset": page * 100, "limit": 100}, timeout=POLL_TIMEOUT)
        if r.status_code == 404:
            break
        r.raise_for_status()
        data = r.json() or {}
        items = data.get("content") or []
        if not items:
            break
        for j in items:
            jid = j.get("id") or j.get("refNumber") or j.get("name")
            title = (j.get("name") or "")
            loc_city = (j.get("location") or {}).get("city") or ""
            loc_country = (j.get("location") or {}).get("countryCode") or ""
            loc = ", ".join([x for x in [loc_city, loc_country] if x]).strip(", ")
            url = j.get("ref") or j.get("applyUrl") or j.get("companyCareerUrl") or ""
            desc = ((j.get("jobAd") or {}).get("sections", {}) or {}).get("jobDescription", {}).get("text", "") or ""
            yield {
                "source": f"smartrecruiters:{company_slug}",
                "id": str(jid),
                "title": title,
                "location": loc,
                "desc": desc,
                "url": url,
            }
        page += 1

def fetch_recruitee(company_slug):
    # Not all companies are on Recruitee; safe to try.
    url = f"https://api.recruitee.com/c/{quote_plus(company_slug)}/careers/offers"
    r = requests.get(url, timeout=POLL_TIMEOUT)
    if r.status_code == 404:
        return
    r.raise_for_status()
    data = r.json() or {}
    for j in data.get("offers", []):
        jid = j.get("id") or j.get("slug") or j.get("title")
        title = j.get("title") or ""
        loc_city = (j.get("location") or {}).get("city") or ""
        loc_cc = (j.get("location") or {}).get("country_code") or ""
        loc = ", ".join([x for x in [loc_city, loc_cc] if x]).strip(", ")
        url = j.get("careers_url") or j.get("apply_url") or ""
        desc = j.get("description") or ""
        yield {
            "source": f"recruitee:{company_slug}",
            "id": str(jid),
            "title": title,
            "location": loc,
            "desc": desc,
            "url": url,
        }
# -------------------------------------------------

def preferred_location_score(conf, location):
    loc = normalize(location or "")
    score = 0
    for i, want in enumerate(conf.get("locations_prefer", [])):
        if normalize(want) in loc:
            score += (100 - i)
    return score

def send_email(conf, items):
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


# ---------- NEW: Amazon & Workday CxS fetchers ----------

def fetch_amazon(params: dict):
    """
    Amazon public jobs endpoint (read-only).
    You can pass filters from config.yaml under companies.amazon.params.
    Example base: https://www.amazon.jobs/en/search.json
    """
    base = params.get("base", "https://www.amazon.jobs/en/search.json")
    # sensible defaults; override in config as needed
    query = {
        "offset": 0,
        "result_limit": 100,
        "sort": "recent",
        # Common filters: "category", "normalized_country_code", "query", "business_category"
        # We'll merge with user-specified params below.
    }
    # merge user params
    for k, v in (params.get("query_params") or {}).items():
        query[k] = v

    seen = 0
    while True:
        r = requests.get(base, params=query, timeout=POLL_TIMEOUT)
        if r.status_code == 404:
            break
        r.raise_for_status()
        data = r.json() or {}
        jobs = data.get("jobs", []) or data.get("hits", []) or []
        if not jobs:
            break

        for j in jobs:
            # Fields differ slightly depending on Amazon endpoint version.
            jid = str(j.get("id") or j.get("job_id") or j.get("posting_id") or j.get("slug") or j.get("title"))
            title = j.get("title") or j.get("normalized_job_title") or ""
            loc = j.get("location") or j.get("city_state_country") or j.get("cityStateCountry") or ""
            url = j.get("url") or j.get("job_path") or ""
            # Many results give only a path; prepend domain if needed
            if url and url.startswith("/"):
                url = "https://www.amazon.jobs" + url
            desc = j.get("description") or j.get("basic_qualifications") or ""
            yield {
                "source": "amazon",
                "id": jid,
                "title": title,
                "location": loc,
                "desc": desc,
                "url": url,
            }

        seen += len(jobs)
        # paginate by bumping offset; stop if fewer than page size returned
        query["offset"] = int(query.get("offset", 0)) + int(query.get("result_limit", 100))
        if len(jobs) < int(query.get("result_limit", 100)):
            break


def fetch_workday_cxs(site: dict):
    """
    Generic Workday CxS reader (read-only).
    Works for many Workday-powered sites that expose the public CxS jobs API.
    You specify host/tenant/org and optional 'search' payload in config.
    Example base: https://<host>/wday/cxs/<tenant>/<org>/jobs
    """
    host = site.get("host")  # e.g., "careers.microsoft.com", "www.metacareers.com"
    tenant = site.get("tenant")  # Workday tenant code (varies by company)
    org = site.get("org")        # Org slug (varies)
    if not host or not tenant or not org:
        return

    base = f"https://{host}/wday/cxs/{quote_plus(tenant)}/{quote_plus(org)}/jobs"
    payload = site.get("search") or {}
    # Typical CxS supports paging via "page" or "limit"/"offset" fields in the POST payload.
    # We'll try a "page" loop if not specified.
    page = 1
    max_pages = int(site.get("max_pages", 30))
    while page <= max_pages:
        # include page if caller didn't specify one
        body = dict(payload)
        body.setdefault("page", page)
        try:
            r = requests.post(base, json=body, timeout=POLL_TIMEOUT)
            if r.status_code == 404:
                break
            r.raise_for_status()
            data = r.json() or {}
        except Exception:
            break

        # Common shapes:
        # data["jobPostings"] or data["jobs"] or data["positions"]
        postings = (
            data.get("jobPostings")
            or data.get("jobs")
            or data.get("positions")
            or []
        )
        if not postings:
            # Some CxS variants nest under data["total"] and data["jobPostings"]
            container = data.get("result" or "data" or "") or {}
            postings = container.get("jobPostings") or []
        if not postings:
            break

        for j in postings:
            jid = str(
                j.get("id")
                or j.get("jobPostingId")
                or j.get("externalUrl")
                or j.get("title")
            )
            title = j.get("title") or j.get("postingTitle") or ""
            loc = (
                j.get("location")
                or j.get("locationsText")
                or j.get("city")
                or ""
            )
            url = j.get("externalUrl") or j.get("absoluteUrl") or j.get("hostedUrl") or ""
            desc = j.get("description") or j.get("jobText") or j.get("postingDescription") or ""

            yield {
                "source": f"workday:{host}",
                "id": jid,
                "title": title,
                "location": loc,
                "desc": desc,
                "url": url,
            }

        # stop if the page returned fewer than expected (best-effort)
        page += 1


def fetch_google_careers(params: dict):
    """
    Google Careers (read-only). Google changes versions occasionally, so we try a few bases.
    You can tweak query in config under companies.google.query_params.
    Common query keys that tend to work: q, page, page_size, skills, degree, location, employment_type.
    """
    bases = params.get("bases") or [
        "https://careers.google.com/api/v3/search/",
        "https://careers.google.com/api/v2/search/",
        "https://careers.google.com/api/v1/search/",
    ]
    q = {
        "page": 1,
        "page_size": 100,
    }
    for k, v in (params.get("query_params") or {}).items():
        q[k] = v

    for base in bases:
        page = 1
        while True:
            q["page"] = page
            try:
                r = requests.get(base, params=q, timeout=POLL_TIMEOUT)
                if r.status_code == 404:
                    break
                r.raise_for_status()
                data = r.json() or {}
            except Exception:
                break

            # Google has used different shapes over time; try a few
            jobs = (
                data.get("jobs") or
                data.get("results") or
                data.get("positions") or
                []
            )
            if not jobs:
                break

            for j in jobs:
                # Try to normalize fields
                jid = str(
                    j.get("id") or
                    j.get("job_id") or
                    j.get("slug") or
                    j.get("apply_url") or
                    j.get("title") or ""
                )
                title = j.get("title") or j.get("job_title") or ""
                # locations might be a string, list of strings, or list of dicts with "display" fields
                loc = j.get("location") or j.get("locations") or ""
                if isinstance(loc, list):
                    loc = ", ".join(
                        [x.get("display") if isinstance(x, dict) else str(x) for x in loc]
                    )
                url = (
                    j.get("apply_url") or
                    j.get("job_url") or
                    j.get("canonical_url") or
                    ""
                )
                desc = (
                    j.get("description") or
                    j.get("description_html") or
                    j.get("summary") or
                    ""
                )

                yield {
                    "source": "google-careers",
                    "id": jid,
                    "title": title,
                    "location": loc or "",
                    "desc": desc or "",
                    "url": url or "",
                }

            # stop if short page
            if len(jobs) < int(q.get("page_size", 100)):
                break
            page += 1



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

        # File-based state (e.g., GitHub Actions)
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

    for org in conf["companies"].get("smartrecruiters", []):
        try:
            for j in fetch_smartrecruiters(org): consider(j)
        except Exception as e:
            print(f"[warn] smartrecruiters {org}: {e}", file=sys.stderr)

    for org in conf["companies"].get("recruitee", []):
        try:
            for j in fetch_recruitee(org): consider(j)
        except Exception as e:
            print(f"[warn] recruitee {org}: {e}", file=sys.stderr)

        # NEW: Amazon (single entry with params)
    
    amazon_conf = conf["companies"].get("amazon")
    if amazon_conf:
        try:
            for j in fetch_amazon(amazon_conf): consider(j)
        except Exception as e:
            print(f"[warn] amazon: {e}", file=sys.stderr)

    # NEW: Workday CxS (list of sites)
    for site in conf["companies"].get("workday_cxs", []):
        try:
            for j in fetch_workday_cxs(site): consider(j)
        except Exception as e:
            host = site.get("host", "<unknown>")
            print(f"[warn] workday_cxs {host}: {e}", file=sys.stderr)

    # NEW: Google Careers (read-only monitor)
    google_conf = conf["companies"].get("google")
    if google_conf:
        try:
            for j in fetch_google_careers(google_conf): consider(j)
        except Exception as e:
            print(f"[warn] google-careers: {e}", file=sys.stderr)

    # Sort for readability
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
