#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Built In job search → email digest (resilient)

- Robust job-link capture on results pages
- Detail hydration from each job page
- URL pattern supports: ?search=...&country=USA&allLocations=true&daysSinceUpdated=1
- SMTP via Gmail 587 STARTTLS; supports MAIL_* or EMAIL_* env names

.env example:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=yourgmail@gmail.com
  SMTP_PASS=your-app-password
  MAIL_FROM=yourgmail@gmail.com
  MAIL_TO=you+jobs@gmail.com

  # Optional Built In login (not required)
  BUILTIN_EMAIL=you@builtin-login
  BUILTIN_PASSWORD=secret
"""

import os, re, ssl, sys, time, html, smtplib
from dataclasses import dataclass
from typing import List, Dict, Set, Tuple
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse, quote_plus
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# =========================
# ===== Configuration =====
# =========================

# Prefer explicit URLs in your Built In format; otherwise KEYWORDS will be used.
SEARCH_URLS: List[str] = [
    # Example:
    # "https://builtin.com/jobs?search=.NET+Engineer&country=USA&allLocations=true&daysSinceUpdated=1",
]

KEYWORDS: List[str] = [
    ".NET Engineer",
    "ASP.NET Core",
    "Full Stack .NET",
]

# Extra search params
REMOTE_ONLY = False                 # adds &remote_only=true
DAYS_SINCE_UPDATED = 1              # set None to disable
POSTED_WITHIN_DAYS = 14             # heuristic using page text; set 0 to disable

# Caps
MAX_PER_SEARCH = 100                # limit hydrated per search
MAX_TOTAL = 300

# Login (optional)
LOGIN_ENABLED = False
BUILTIN_EMAIL = os.getenv("BUILTIN_EMAIL")
BUILTIN_PASSWORD = os.getenv("BUILTIN_PASSWORD")

# Browser
HEADLESS = False  # set True after confirming it works
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

# SMTP (supports MAIL_* or EMAIL_*)
SMTP = {
    "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "port": int(os.getenv("SMTP_PORT", "587")),  # STARTTLS
    "user": os.getenv("SMTP_USER"),
    "pass": os.getenv("SMTP_PASS"),
    "to":   os.getenv("MAIL_TO") or os.getenv("EMAIL_TO"),
    "from": os.getenv("MAIL_FROM") or os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER"),
}

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
    base = "https://builtin.com/jobs"
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
    p = urlparse(url)
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
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))

def build_targets() -> List[Tuple[str, str]]:
    targets: List[Tuple[str, str]] = []
    if SEARCH_URLS:
        for u in SEARCH_URLS:
            targets.append((normalize_search_url(u), "(URL)"))
    else:
        for kw in KEYWORDS:
            targets.append((build_search_url_from_keyword(kw), kw))
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

# Accept both /job/... and /jobs/... patterns; require a numeric id chunk
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
    if m:
        return int(m.group(1)) <= POSTED_WITHIN_DAYS
    m = re.search(r"(\d+)\s+week", t)
    if m:
        return (int(m.group(1)) * 7) <= POSTED_WITHIN_DAYS
    return True

def send_email(subject: str, html_body: str) -> None:
    if not all([SMTP["host"], SMTP["port"], SMTP["user"], SMTP["pass"], SMTP["to"], SMTP["from"]]):
        raise RuntimeError("SMTP configuration incomplete (need SMTP_*, MAIL_TO/MAIL_FROM or EMAIL_*).")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP["from"]
    msg["To"] = SMTP["to"]
    msg.attach(MIMEText(html_body, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP["host"], SMTP["port"]) as server:
        server.starttls(context=ctx)  # Gmail 587
        server.login(SMTP["user"], SMTP["pass"])
        server.sendmail(SMTP["from"], [SMTP["to"]], msg.as_string())

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
# ===== Scraper ===========
# =========================

def builtin_login(page) -> None:
    if not LOGIN_ENABLED or not (BUILTIN_EMAIL and BUILTIN_PASSWORD):
        print("Login skipped.")
        return
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
    """
    Collect job links from cards; click 'Load more' if present; fallback to all anchors.
    Only keep links that match real job URL shapes (with numeric id).
    """
    results: List[Job] = []
    seen: Set[str] = set()

    accept_cookies(page)
    page.wait_for_timeout(600)

    # Try to reveal more rows
    for _ in range(4):
        try:
            btn = page.query_selector('button:has-text("Load more"), button:has-text("Load More")')
            if btn:
                btn.click()
                page.wait_for_timeout(900)
        except Exception:
            pass
        page.mouse.wheel(0, 25000)
        page.wait_for_timeout(500)

    # 1) Prefer anchors inside card containers
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
    if results:
        return results

    # 2) Fallback: scan ALL anchors with /job or /jobs
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

            # title
            title_el = page.query_selector("h1") or page.query_selector("h2")
            title = (title_el.inner_text().strip() if title_el else "")
            if not title:
                mt = page.query_selector('meta[property="og:title"]')
                if mt:
                    title = (mt.get_attribute("content") or "").strip()

            # company
            company_el = (
                page.query_selector('a[href^="/company/"]')
                or page.query_selector('[data-qa="company"]')
                or page.query_selector('[class*="company"]')
            )
            company = (company_el.inner_text().strip() if company_el else "")

            # posted
            posted_el = page.query_selector("time") or page.query_selector('[class*="posted"]') \
                         or page.query_selector('[data-qa="posted"]')
            posted = (posted_el.inner_text().strip() if posted_el else "")

            # location
            loc_el = page.query_selector('[data-qa="location"]') or page.query_selector('[class*="location"]') \
                     or page.query_selector('a[href*="locations"]')
            location = (loc_el.inner_text().strip() if loc_el else "")

            if not looks_recent(posted):
                page.close()
                continue

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
                # extra retries
                for _ in range(2):
                    page.mouse.wheel(0, 25000)
                    page.wait_for_timeout(800)
                found = parse_jobs_on_page(page, matched_on=label)

            print(f"DEBUG: found {len(found)} job links on results page")
            if not found:
                anchors = [a.get_attribute("href") or "" for a in page.query_selector_all("a[href]")]
                samples = [u for u in anchors if "/job" in u or "/jobs" in u][:20]
                print("DEBUG sample anchors:", samples)

            found = hydrate_from_detail(context, found)

            for j in found:
                if len(all_jobs) >= MAX_TOTAL:
                    break
                all_jobs.setdefault(j.url, j)

            if len(all_jobs) >= MAX_TOTAL:
                break

        browser.close()

    return sorted(all_jobs.values(), key=sort_key)

def main():
    jobs = run_searches()
    if not jobs:
        print("No jobs found with the current filters.")
        return
    subject = f"[Built In] {len(jobs)} matches • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    body = render_email(jobs)
    send_email(subject, body)
    print(f"Sent {len(jobs)} jobs to {SMTP['to']}.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
