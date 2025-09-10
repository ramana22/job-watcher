#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Built In job search → email digest (resilient + cross-run dedupe)

- Supports explicit SEARCH_URLS (like your all-levels link) OR
  dynamic building from KEYWORDS + chosen experience levels.
- Robust link capture + detail-page hydration (title/company/location/posted).
- Email via Gmail 587 STARTTLS (MAIL_* or EMAIL_* envs).
- Dedupe:
  * SQLite mode (default): never re-send same URL; suppress company for N days
  * File mode: set STATE_FILE to use a simple text file of seen URLs
"""

import os, re, ssl, sys, time, html, smtplib, sqlite3, json
from dataclasses import dataclass
from typing import List, Dict, Set, Tuple
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse, quote_plus
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# =========================
# ===== Configuration =====
# =========================

# --- If you want to hard-wire your all-levels link, keep it here ---
SEARCH_URLS: List[str] = [
    "https://builtin.com/jobs/internship/entry-level/junior/mid-level/senior/expert-leader?search=.Net&daysSinceUpdated=1&country=USA&allLocations=true"
]

# If SEARCH_URLS is empty, we’ll build from KEYWORDS + EXP_LEVELS / EXP_LEVELS_JSON
KEYWORDS: List[str] = [
    ".Net", "C#", "ASP.NET", "Full Stack .NET"
]

REMOTE_ONLY = False                 # adds &remote_only=true to query
DAYS_SINCE_UPDATED = 1              # set None to disable
POSTED_WITHIN_DAYS = 14             # text heuristic on job page; set 0 to disable

MAX_PER_SEARCH = 100                # max hydrated per search
MAX_TOTAL = 300                     # global cap

LOGIN_ENABLED = False               # BuiltIn login not required
BUILTIN_EMAIL = os.getenv("BUILTIN_EMAIL")
BUILTIN_PASSWORD = os.getenv("BUILTIN_PASSWORD")

HEADLESS = True                     # set False locally to watch runs
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

SMTP = {
    "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "port": int(os.getenv("SMTP_PORT", "587")),
    "user": os.getenv("SMTP_USER"),
    "pass": os.getenv("SMTP_PASS"),
    "to":   os.getenv("MAIL_TO") or os.getenv("EMAIL_TO"),
    "from": os.getenv("MAIL_FROM") or os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER"),
}

# =========================
# ===== Dedupe config =====
# =========================

# If STATE_FILE is set -> file-based dedupe (one URL per line).
# Else -> SQLite (URL forever + company squelch).
STATE_FILE = os.getenv("STATE_FILE", "").strip()

DB_PATH = "builtin_jobs.sqlite"
SQUELCH_COMPANY_DAYS = 30
ALLOW_UNKNOWN_COMPANY = True

def norm_company(name: str) -> str:
    return (name or "").strip().lower()

# =========================
# === Experience levels ===
# =========================
# If you clear SEARCH_URLS, we can build /jobs/<levels...> from EXP_LEVELS(_JSON)

LEVEL_SLUGS = {
    "intern": "internship", "internship": "internship",

    "entry": "entry-level", "entry level": "entry-level", "entry-level": "entry-level",
    "0-1": "entry-level", "0-1 years": "entry-level",

    "junior": "junior", "1-2": "junior", "1-2 years": "junior",

    "mid": "mid-level", "mid level": "mid-level", "mid-level": "mid-level",
    "2-5": "mid-level", "2-5 years": "mid-level",

    "senior": "senior-level", "senior level": "senior-level", "senior-level": "senior-level",
    "5-9": "senior-level", "5-9 years": "senior-level",

    "expert": "expert-leader", "leader": "expert-leader", "expert/leader": "expert-leader",
    "expert-leader": "expert-leader", "9+": "expert-leader", "9+ years": "expert-leader",
}

def _clean_level_token(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("(", " ").replace(")", " ").replace("years", "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace("–", "-").replace("—", "-")
    return s

def _levels_to_slugs(level_inputs: List[str]) -> List[str]:
    slugs, seen = [], set()
    for raw in level_inputs:
        key = _clean_level_token(raw)
        candidates = [key, key.replace(" level", ""), key.replace("-level", " level")]
        matched = False
        for c in candidates:
            if c in LEVEL_SLUGS:
                slug = LEVEL_SLUGS[c]
                if slug not in seen:
                    seen.add(slug); slugs.append(slug)
                matched = True
                break
        if not matched:
            m = re.search(r"\b(\d+\s*-\s*\d+|\d+\+)\b", key)
            if m:
                rng = m.group(1).replace(" ", "")
                if rng in LEVEL_SLUGS:
                    slug = LEVEL_SLUGS[rng]
                    if slug not in seen:
                        seen.add(slug); slugs.append(slug)
    return slugs

# Read EXP_LEVELS from env (optional)
EXP_LEVELS: List[str] = []
_exp_json = os.getenv("EXP_LEVELS_JSON")
if _exp_json:
    try:
        EXP_LEVELS = json.loads(_exp_json)
    except Exception:
        print("WARN: EXP_LEVELS_JSON could not be parsed; ignoring.", file=sys.stderr)
else:
    _exp_csv = os.getenv("EXP_LEVELS", "")
    if _exp_csv.strip():
        EXP_LEVELS = [t.strip() for t in _exp_csv.split(",") if t.strip()]

EXP_LEVEL_SLUGS: List[str] = _levels_to_slugs(EXP_LEVELS)

# =========================
# ===== Env overrides =====
# =========================

HEADLESS = os.getenv("HEADLESS", str(HEADLESS)).lower() in ("1", "true", "yes")
REMOTE_ONLY = os.getenv("REMOTE_ONLY", str(REMOTE_ONLY)).lower() in ("1", "true", "yes")
SQUELCH_COMPANY_DAYS = int(os.getenv("SQUELCH_COMPANY_DAYS", SQUELCH_COMPANY_DAYS))
POSTED_WITHIN_DAYS = int(os.getenv("POSTED_WITHIN_DAYS", POSTED_WITHIN_DAYS))
DB_PATH = os.getenv("DB_PATH", DB_PATH)

_tmp = os.getenv("DAYS_SINCE_UPDATED")
if _tmp is not None and _tmp != "":
    DAYS_SINCE_UPDATED = None if _tmp.lower() == "none" else int(_tmp)

kw_json = os.getenv("KEYWORDS_JSON")
if kw_json:
    try:
        KEYWORDS = json.loads(kw_json)
    except Exception:
        print("WARN: KEYWORDS_JSON parse failed; using defaults.", file=sys.stderr)

urls_json = os.getenv("SEARCH_URLS_JSON")
if urls_json:
    try:
        SEARCH_URLS = json.loads(urls_json)
    except Exception:
        print("WARN: SEARCH_URLS_JSON parse failed; using defaults.", file=sys.stderr)

# =========================
# ===== Data Model ========
# =========================

@dataclass(frozen=True)
class Job:
    title: str
    company: str
    location: str
    posted: str
    url: str
    matched_on: str  # keyword or "(URL)"

# =========================
# ===== URL Helpers =======
# =========================

def build_search_url_from_keyword(keyword: str) -> str:
    """
    Build URL from keyword + optional EXP_LEVEL_SLUGS -> /jobs/<slugs...>
    (only used when SEARCH_URLS is empty).
    """
    path = "/jobs"
    if EXP_LEVEL_SLUGS:
        path += "/" + "/".join(EXP_LEVEL_SLUGS)

    base = f"https://builtin.com{path}"
    params = {
        "search": keyword,
        "country": "USA",
        "allLocations": "true",
        "per_page": str(MAX_PER_SEARCH),
        "sort": "recent",
        "status": "all",
    }
    if REMOTE_ONLY:
        params["remote_only"] = "true"
    if DAYS_SINCE_UPDATED is not None:
        params["daysSinceUpdated"] = str(DAYS_SINCE_UPDATED)
    return f"{base}?{urlencode(params, quote_via=quote_plus)}"

def normalize_search_url(url: str) -> str:
    """
    If caller already provided a full category path (e.g., /jobs/internship/entry-level/...),
    we keep it. If they passed /jobs only and EXP_LEVEL_SLUGS exist, we inject them.
    (Your provided URL is preserved.)
    """
    p = urlparse(url)
    if (p.path.rstrip("/") == "/jobs") and EXP_LEVEL_SLUGS:
        new_path = "/jobs/" + "/".join(EXP_LEVEL_SLUGS)
    else:
        new_path = p.path

    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q.setdefault("country", "USA")
    q.setdefault("allLocations", "true")
    q.setdefault("per_page", str(MAX_PER_SEARCH))
    q.setdefault("sort", "recent")
    q.setdefault("status", "all")
    if REMOTE_ONLY:
        q["remote_only"] = "true"
    if DAYS_SINCE_UPDATED is not None:
        q["daysSinceUpdated"] = str(DAYS_SINCE_UPDATED)

    new_q = urlencode(q, doseq=True, quote_via=quote_plus)
    return urlunparse((p.scheme, p.netloc, new_path, p.params, new_q, p.fragment))

def build_targets() -> List[Tuple[str, str]]:
    targets: List[Tuple[str, str]] = []
    level_label = f" [levels: {', '.join(EXP_LEVEL_SLUGS)}]" if EXP_LEVEL_SLUGS else ""
    if SEARCH_URLS:
        for u in SEARCH_URLS:
            targets.append((normalize_search_url(u), f"(URL){level_label}"))
    else:
        for kw in KEYWORDS:
            targets.append((build_search_url_from_keyword(kw), f"{kw}{level_label}"))
    return targets

# =========================
# ===== Utilities =========
# =========================

def accept_cookies(page) -> None:
    try:
        selectors = [
            'button:has-text("Accept All")',
            'button:has-text("Accept all")',
            'button:has-text("I Accept")',
            'button:has-text("Accept")',
            '[aria-label*="Accept"]',
        ]
        for sel in selectors:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                page.wait_for_timeout(200)
                break
    except Exception:
        pass

REAL_JOB_HREF = re.compile(
    r"^https?://builtin\.com/(job|jobs)/[^?#]*\d{4,}[^?#]*(?:\?.*)?$",
    re.IGNORECASE,
)

def is_real_job_link(href: str) -> bool:
    if not href:
        return False
    if href.startswith("/"):
        href = "https://builtin.com" + href
    return bool(REAL_JOB_HREF.match(href))

def looks_recent(posted_text: str) -> bool:
    if POSTED_WITHIN_DAYS <= 0:
        return True
    t = (posted_text or "").strip().lower()
    if not t or "today" in t or "hour" in t:
        return True
    m = re.search(r"(\d+)\s+day", t)
    if m and int(m.group(1)) <= POSTED_WITHIN_DAYS:
        return True
    m = re.search(r"(\d+)\s+week", t)
    if m and (int(m.group(1)) * 7) <= POSTED_WITHIN_DAYS:
        return True
    return True  # be lenient

def send_email(subject: str, html_body: str) -> None:
    if not all([SMTP["host"], SMTP["port"], SMTP["user"], SMTP["pass"], SMTP["to"], SMTP["from"]]):
        raise RuntimeError("SMTP configuration incomplete (need SMTP_*, MAIL_TO/MAIL_FROM or EMAIL_*).")

    recipients = [addr.strip() for addr in SMTP["to"].split(",") if addr.strip()]
    # Optional hard-coded extra recipient:
    # extra_recipient = "someone@example.com"
    # if extra_recipient and extra_recipient not in recipients:
    #     recipients.append(extra_recipient)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Ramana Job Bot", SMTP["from"]))
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP["host"], SMTP["port"]) as server:
        server.starttls(context=ctx)
        server.login(SMTP["user"], SMTP["pass"])
        server.sendmail(SMTP["from"], recipients, msg.as_string())

def render_email(jobs: List[Job]) -> str:
    rows = []
    for j in jobs:
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(j.url)}'>{html.escape(j.title or '(No title)')}</a></td>"
            f"<td>{html.escape(j.company or '')}</td>"
            f"<td>{html.escape(j.location or '')}</td>"
            f"<td>{html.escape(j.posted or '')}</td>"
            f"<td>{html.escape(j.matched_on or '')}</td>"
            "</tr>"
        )
    return (
        "<p>Here are your latest Built In matches.</p>"
        "<table border='1' cellspacing='0' cellpadding='6'>"
        "<thead><tr>"
        "<th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Matched On</th>"
        "</tr></thead><tbody>"
        + "".join(rows) +
        "</tbody></table>"
        f"<p>Total: {len(jobs)}</p>"
    )

# =========================
# ==== File-based state ===
# =========================

def load_seen_file() -> Set[str]:
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}

def save_seen_file(seen: Set[str]) -> None:
    if not STATE_FILE:
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for url in sorted(seen):
            f.write(url + "\n")

# =========================
# ===== SQLite dedupe =====
# =========================

def db_init(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sent_jobs (
            url TEXT PRIMARY KEY,
            company_norm TEXT,
            company_raw TEXT,
            title TEXT,
            sent_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sent_companies (
            company_norm TEXT PRIMARY KEY,
            last_sent_at TEXT
        )
    """)
    con.commit()
    return con

def db_already_sent_url(con: sqlite3.Connection, url: str) -> bool:
    cur = con.execute("SELECT 1 FROM sent_jobs WHERE url = ? LIMIT 1", (url,))
    return cur.fetchone() is not None

def db_company_recently_sent(con: sqlite3.Connection, company_norm_val: str, days: int) -> bool:
    if not company_norm_val:
        return False
    cur = con.execute("SELECT last_sent_at FROM sent_companies WHERE company_norm = ?", (company_norm_val,))
    row = cur.fetchone()
    if not row:
        return False
    last = datetime.fromisoformat(row[0])
    return datetime.utcnow() - last < timedelta(days=days)

def db_mark_sent(con: sqlite3.Connection, jobs: List[Job]) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    for j in jobs:
        c_norm = norm_company(j.company)
        con.execute(
            "INSERT OR IGNORE INTO sent_jobs (url, company_norm, company_raw, title, sent_at) VALUES (?,?,?,?,?)",
            (j.url, c_norm, j.company or "", j.title or "", now)
        )
        if c_norm:
            con.execute(
                "INSERT INTO sent_companies (company_norm, last_sent_at) VALUES (?,?) "
                "ON CONFLICT(company_norm) DO UPDATE SET last_sent_at=excluded.last_sent_at",
                (c_norm, now)
            )
    con.commit()

def filter_new_jobs_sqlite(con: sqlite3.Connection, jobs: List[Job]) -> List[Job]:
    filtered: List[Job] = []
    for j in jobs:
        if db_already_sent_url(con, j.url):
            continue
        c_norm = norm_company(j.company)
        if not c_norm and ALLOW_UNKNOWN_COMPANY:
            filtered.append(j); continue
        if db_company_recently_sent(con, c_norm, SQUELCH_COMPANY_DAYS):
            continue
        filtered.append(j)
    return filtered

# =========================
# ===== Scraper ===========
# =========================

def builtin_login(page) -> None:
    if not LOGIN_ENABLED or not (BUILTIN_EMAIL and BUILTIN_PASSWORD):
        print("Login skipped."); return
    try:
        page.goto("https://builtin.com/users/sign_in", timeout=60000)
        page.wait_for_selector('input[name="email"]', timeout=30000)
        page.fill('input[name="email"]', BUILTIN_EMAIL)
        page.fill('input[name="password"]', BUILTIN_PASSWORD)
        page.click('button[type="submit"]')
        try:
            page.wait_for_selector('a[href^="/profile"]', timeout=30000)
        except PWTimeout:
            time.sleep(1)
    except Exception as e:
        print(f"Login warning: {e}. Continuing without login.")

def parse_jobs_on_page(page, matched_on: str) -> List[Job]:
    results: List[Job] = []
    seen: Set[str] = set()

    accept_cookies(page)
    page.wait_for_timeout(600)

    # Try to reveal more rows
    for _ in range(4):
        try:
            btn = page.query_selector('button:has-text("Load more"), button:has-text("Load More")')
            if btn:
                btn.click(); page.wait_for_timeout(900)
        except Exception:
            pass
        page.mouse.wheel(0, 25000); page.wait_for_timeout(500)

    # Prefer anchors inside card containers
    card_selectors = [
        'article[data-entity-type="job"] a[href^="/job"]',
        'article[data-entity-type="job"] a[href^="/jobs"]',
        'li[class*="jobs-list"] article a[href^="/job"]',
        'li[class*="jobs-list"] article a[href^="/jobs"]',
        'div[class*="job-card"] a[href^="/job"]',
        'div[class*="job-card"] a[href^="/jobs"]',
    ]

    def add_link(a):
        href = (a.get_attribute("href") or "").strip()
        if href.startswith("/"):
            href = "https://builtin.com" + href
        if not is_real_job_link(href) or href in seen:
            return
        seen.add(href)
        title = (a.inner_text() or "").strip()
        results.append(Job(title=title or "", company="", location="", posted="", url=href, matched_on=matched_on))

    for sel in card_selectors:
        for a in page.query_selector_all(sel):
            add_link(a)

    if not results:
        for a in page.query_selector_all('a[href*="/job"], a[href*="/jobs"]'):
            add_link(a)

    return results

def hydrate_from_detail(context, jobs: List[Job]) -> List[Job]:
    hydrated: List[Job] = []
    for j in jobs[:MAX_PER_SEARCH]:
        page = context.new_page()
        try:
            page.goto(j.url, timeout=60000)
            accept_cookies(page)
            page.wait_for_selector("h1, h2, title", timeout=15000)

            title_el = page.query_selector("h1") or page.query_selector("h2")
            title = (title_el.inner_text().strip() if title_el else "")
            if not title:
                mt = page.query_selector('meta[property="og:title"]')
                if mt:
                    title = (mt.get_attribute("content") or "").strip()

            company_el = (
                page.query_selector('a[href^="/company/"]')
                or page.query_selector('[data-qa="company"]')
                or page.query_selector('[class*="company"]')
            )
            company = (company_el.inner_text().strip() if company_el else "")

            posted_el = (
                page.query_selector("time")
                or page.query_selector('[class*="posted"]')
                or page.query_selector('[data-qa="posted"]')
            )
            posted = (posted_el.inner_text().strip() if posted_el else "")

            loc_el = (
                page.query_selector('[data-qa="location"]')
                or page.query_selector('[class*="location"]')
                or page.query_selector('a[href*="locations"]')
            )
            location = (loc_el.inner_text().strip() if loc_el else "")

            if not looks_recent(posted):
                page.close(); continue

            hydrated.append(Job(
                title=title or j.title or "(No title)",
                company=company or "(Unknown company)",
                location=location,
                posted=posted,
                url=j.url,
                matched_on=j.matched_on,
            ))
        except Exception:
            hydrated.append(Job(
                title=j.title or "(No title)",
                company=j.company or "(Unknown company)",
                location=j.location,
                posted=j.posted,
                url=j.url,
                matched_on=j.matched_on,
            ))
        finally:
            page.close()
    return hydrated

def sort_key(j: Job) -> Tuple[int, str]:
    p = (j.posted or "").lower()
    score = 999
    if "hour" in p or "today" in p:
        score = 0
    elif "day" in p:
        m = re.search(r"(\d+)\s+day", p)
        score = int(m.group(1)) if m else 3
    elif "week" in p:
        m = re.search(r"(\d+)\s+week", p)
        score = 7 * (int(m.group(1)) if m else 1)
    return (score, (j.title or "").lower())

def run_searches() -> List[Job]:
    all_jobs: Dict[str, Job] = {}
    targets = build_targets()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            context = browser.new_context(
                user_agent=REAL_UA,
                viewport={"width": 1366, "height": 900},
                java_script_enabled=True,
            )
            page = context.new_page()

            builtin_login(page)

            for url, label in targets:
                print(f"Search → {label if label != '(URL)' else 'URL'}: {url}")
                page.goto(url, timeout=60000)
                accept_cookies(page)
                try:
                    page.wait_for_selector("a[href*='/job'], a[href*='/jobs'], article", timeout=15000)
                except PWTimeout:
                    pass

                found = parse_jobs_on_page(page, matched_on=label)
                if not found:
                    for _ in range(2):
                        page.mouse.wheel(0, 25000); page.wait_for_timeout(800)
                    found = parse_jobs_on_page(page, matched_on=label)

                print(f"DEBUG: found {len(found)} job links on results page")

                found = hydrate_from_detail(context, found)

                for j in found:
                    if len(all_jobs) >= MAX_TOTAL:
                        break
                    all_jobs.setdefault(j.url, j)

                if len(all_jobs) >= MAX_TOTAL:
                    break
        finally:
            try: browser.close()
            except Exception: pass

    return sorted(all_jobs.values(), key=sort_key)

# =========================
# ========= Main ==========
# =========================

def main():
    jobs = run_searches()
    if not jobs:
        print("No jobs found with the current filters.")
        return

    if STATE_FILE:
        seen = load_seen_file()
        fresh_jobs = [j for j in jobs if j.url not in seen]
        if not fresh_jobs:
            print(f"Nothing new to email (file dedupe: {STATE_FILE}).")
            return
        subject = f"[Built In] {len(fresh_jobs)} new matches • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        body = render_email(fresh_jobs)
        send_email(subject, body)
        seen.update(j.url for j in fresh_jobs)
        save_seen_file(seen)
        print(f"Emailed {len(fresh_jobs)} new jobs to {SMTP['to']} and recorded in {STATE_FILE}.")
        return

    con = db_init(DB_PATH)
    fresh_jobs = filter_new_jobs_sqlite(con, jobs)
    if not fresh_jobs:
        print("Nothing new to email (SQLite dedupe).")
        return
    subject = f"[Built In] {len(fresh_jobs)} new matches • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    body = render_email(fresh_jobs)
    send_email(subject, body)
    db_mark_sent(con, fresh_jobs)
    print(f"Emailed {len(fresh_jobs)} new jobs to {SMTP['to']} and recorded in {DB_PATH}.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
