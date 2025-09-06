#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Built In job search → email results

- Logs in (optional) using Playwright
- Searches for multiple keywords/locations
- Scrapes results
- Emails an HTML summary

Setup:
  pip install playwright python-dotenv
  playwright install

Env vars (or .env file in same folder):
  BUILTIN_EMAIL=you@example.com
  BUILTIN_PASSWORD=your-password
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=465
  SMTP_USER=you@gmail.com
  SMTP_PASS=your-app-password
  EMAIL_TO=you@gmail.com
  EMAIL_FROM=you@gmail.com
"""

import os, re, smtplib, ssl, sys, time, html
from dataclasses import dataclass, asdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Set, Tuple
from urllib.parse import urlencode, quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from dotenv import load_dotenv

load_dotenv()

# ----------- Configuration -----------
KEYWORDS = [
    "C# .NET", "ASP.NET Core", "Full Stack .NET", "Azure .NET", "Power BI .NET"
]
# Use Built In “location” chips. Empty list = no filter by location.
LOCATIONS = [
    # Examples: "Dallas-Fort Worth", "Remote"
    "Remote",
]
# Additional search flags:
REMOTE_ONLY = True          # Built In supports remote filters in the URL
POSTED_WITHIN_DAYS = 14     # filter out older postings (best-effort)
MAX_PER_SEARCH = 100        # per search page size (Built In accepts larger per_page)
MAX_TOTAL = 300             # safety cap across all searches

LOGIN_ENABLED = True        # Set False to skip login (works for public search)
HEADLESS = True             # set to False to watch it run

SMTP = {
    "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "user": os.getenv("SMTP_USER"),
    "pass": os.getenv("SMTP_PASS"),
    "to":   os.getenv("EMAIL_TO"),
    "from": os.getenv("EMAIL_FROM", os.getenv("SMTP_USER")),
}

BUILTIN_EMAIL = os.getenv("BUILTIN_EMAIL")
BUILTIN_PASSWORD = os.getenv("BUILTIN_PASSWORD")

# ----------- Data Models -----------
@dataclass(frozen=True)
class Job:
    title: str
    company: str
    location: str
    posted: str
    url: str
    keyword: str

# ----------- Helpers -----------
def build_search_url(keyword: str, location: str | None) -> str:
    """
    Build a Built In jobs search URL. We lean on query params so we don’t rely on brittle UI selectors.
    """
    base = "https://builtin.com/jobs"
    params = {
        "search": keyword,
        "per_page": str(MAX_PER_SEARCH),
        "sort": "recent",
        "status": "all",
    }
    if REMOTE_ONLY:
        params["remote_only"] = "true"
    if location and location.strip():
        # The website also supports location scoping in the path for some markets,
        # but the query param approach is broadly reliable.
        params["locations"] = location

    return f"{base}?{urlencode(params)}"

def looks_recent(posted_text: str) -> bool:
    """
    Very light “posted within X days” filter using typical Built In text like:
    '1 day ago', '3 days ago', 'about 9 hours ago', 'today', etc.
    """
    text = posted_text.strip().lower()
    if not text:
        return True
    if "today" in text or "hour" in text:
        return True
    m = re.search(r"(\d+)\s+day", text)
    if m:
        return int(m.group(1)) <= POSTED_WITHIN_DAYS
    # If weeks appear, approximate
    m = re.search(r"(\d+)\s+week", text)
    if m:
        return int(m.group(1)) * 7 <= POSTED_WITHIN_DAYS
    return True

def send_email(subject: str, html_body: str) -> None:
    if not all([SMTP["host"], SMTP["port"], SMTP["user"], SMTP["pass"], SMTP["to"], SMTP["from"]]):
        raise RuntimeError("SMTP configuration incomplete. Set SMTP_* and EMAIL_TO/EMAIL_FROM env vars.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP["from"]
    msg["To"] = SMTP["to"]
    msg.attach(MIMEText(html_body, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP["host"], SMTP["port"], context=ctx) as server:
        server.login(SMTP["user"], SMTP["pass"])
        server.sendmail(SMTP["from"], [SMTP["to"]], msg.as_string())

def render_email(jobs: List[Job]) -> str:
    rows = []
    for j in jobs:
        rows.append(
            f"<tr>"
            f"<td><a href='{html.escape(j.url)}'>{html.escape(j.title)}</a></td>"
            f"<td>{html.escape(j.company)}</td>"
            f"<td>{html.escape(j.location)}</td>"
            f"<td>{html.escape(j.posted)}</td>"
            f"<td>{html.escape(j.keyword)}</td>"
            f"</tr>"
        )
    table = (
        "<p>Here are your latest Built In matches.</p>"
        "<table border='1' cellspacing='0' cellpadding='6'>"
        "<thead><tr>"
        "<th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Matched On</th>"
        "</tr></thead>"
        "<tbody>"
        + "".join(rows) +
        "</tbody></table>"
        f"<p>Total: {len(jobs)}</p>"
    )
    return table

# ----------- Scraper -----------
def builtin_login(page) -> None:
    """
    Best-effort login flow. If the UI changes, you can comment this out and searches will still work.
    """
    if not (BUILTIN_EMAIL and BUILTIN_PASSWORD):
        print("Login skipped: BUILTIN_EMAIL/BUILTIN_PASSWORD not set.")
        return

    print("Logging in to Built In...")
    page.goto("https://builtin.com/users/sign_in", timeout=60000)
    # Basic form; Built In sometimes uses SSO—this flow covers the simple email/password form.
    page.wait_for_selector('input[name="email"]', timeout=30000)
    page.fill('input[name="email"]', BUILTIN_EMAIL)
    page.fill('input[name="password"]', BUILTIN_PASSWORD)
    # Click sign in
    page.click('button[type="submit"]')
    # Wait for navigation or account menu
    try:
        page.wait_for_selector('a[href^="/profile"]', timeout=30000)
    except PWTimeout:
        # fallback: just wait a moment; searches can still work anonymously
        time.sleep(2)
    print("Login flow finished (proceeding).")

def parse_jobs_on_page(page, keyword: str) -> List[Job]:
    """
    Extract job cards. We use several selector fallbacks to be resilient to minor DOM changes.
    """
    jobs: List[Job] = []

    # Wait for some results to appear (best-effort)
    page.wait_for_timeout(1000)

    # Try common patterns for job cards and links
    card_selectors = [
        'article[data-entity-type="job"]',  # semantic card
        'div[class*="job-card"]',
        'li[class*="jobs-list"] article',
        'article',  # broad fallback
    ]
    seen_in_this_page: Set[str] = set()

    for sel in card_selectors:
        cards = page.query_selector_all(sel)
        if not cards:
            continue
        for c in cards:
            # Link
            link = c.query_selector('a[href*="/jobs/"], a[href*="/job/"]')
            if not link:
                link = c.query_selector("a")
            if not link:
                continue
            href = link.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://builtin.com" + href
            if not href or href in seen_in_this_page:
                continue

            title = (link.inner_text() or "").strip()

            # Company
            company_el = c.query_selector('a[href*="/company/"], [class*="company"], [data-qa="company"]')
            company = (company_el.inner_text().strip() if company_el else "").strip()

            # Location
            loc_el = c.query_selector('[class*="location"], [data-qa="location"]')
            location = (loc_el.inner_text().strip() if loc_el else "").strip()

            # Posted
            posted_el = c.query_selector('[class*="posted"], time, [data-qa="posted"]')
            posted = (posted_el.inner_text().strip() if posted_el else "").strip()

            if POSTED_WITHIN_DAYS and posted and not looks_recent(posted):
                continue

            jobs.append(Job(
                title=title or "(No title)",
                company=company or "(Unknown company)",
                location=location or "",
                posted=posted or "",
                url=href,
                keyword=keyword,
            ))
            seen_in_this_page.add(href)

        if jobs:
            break  # we found a selector that works; stop trying others

    return jobs

def run_searches() -> List[Job]:
    all_jobs: Dict[str, Job] = {}  # key by URL
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        if LOGIN_ENABLED:
            try:
                builtin_login(page)
            except Exception as e:
                print(f"Login warning: {e}. Continuing without login.")

        for kw in KEYWORDS:
            # If no locations specified, run once with None.
            targets = LOCATIONS or [None]
            for loc in targets:
                url = build_search_url(kw, loc)
                print(f"Search → {kw!r} | {loc or 'Any location'}")
                page.goto(url, timeout=60000)
                # wait for jobs container-ish to load
                try:
                    page.wait_for_selector("a[href*='/jobs/'], article", timeout=15000)
                except PWTimeout:
                    pass

                # attempt lazy load / infinite scroll a bit
                for _ in range(3):
                    page.mouse.wheel(0, 20000)
                    page.wait_for_timeout(700)

                found = parse_jobs_on_page(page, kw)

                for j in found:
                    all_jobs.setdefault(j.url, j)
                    if len(all_jobs) >= MAX_TOTAL:
                        break
                if len(all_jobs) >= MAX_TOTAL:
                    break
            if len(all_jobs) >= MAX_TOTAL:
                break

        browser.close()

    # Sort: newest-looking first by posted text heuristic (hours/today first), then title
    def sort_key(j: Job) -> Tuple[int, str]:
        p = j.posted.lower()
        score = 0
        if "hour" in p or "today" in p:
            score = 0
        elif "day" in p:
            m = re.search(r"(\d+)\s+day", p)
            score = int(m.group(1)) if m else 3
        elif "week" in p:
            m = re.search(r"(\d+)\s+week", p)
            score = 7 * (int(m.group(1)) if m else 1)
        else:
            score = 999
        return (score, j.title.lower())

    jobs_sorted = sorted(all_jobs.values(), key=sort_key)
    return jobs_sorted

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
