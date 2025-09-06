#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Built In job search → email digest (robust scraper)

- Optional login (via .env)
- Accepts exact search URLs (your pattern) OR keywords that build that pattern
- Adds per_page, sort, status, and optional daysSinceUpdated
- Collects job links from search results reliably
- Hydrates details by opening each job detail page
- Sends an HTML email with results

Install:
  pip install playwright python-dotenv
  playwright install
"""

import os
import re
import ssl
import sys
import time
import html
import smtplib
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

# Provide your exact Built In search URLs here (preferred). Leave empty to use KEYWORDS.
SEARCH_URLS: List[str] = [
    # Example:
    # "https://builtin.com/jobs?search=.NET+Engineer&country=USA&allLocations=true&daysSinceUpdated=1",
    "https://builtin.com/jobs?search=.NET+Engineer&daysSinceUpdated=1&country=USA&allLocations=true"
]

# If SEARCH_URLS is empty, these keywords will be used to build URLs in your format.
KEYWORDS: List[str] = [
    ".NET Engineer",
    "ASP.NET Core",
    "Full Stack .NET",
]

# Add &remote_only=true to URLs (optional).
REMOTE_ONLY = False

# Only keep postings that *look* recent based on visible text on the page (best-effort).
POSTED_WITHIN_DAYS = 14

# Search page size and safety caps
MAX_PER_SEARCH = 100
MAX_TOTAL = 300

# Number of days since job was updated; set to None to disable.
DAYS_SINCE_UPDATED = 1  # e.g., 1 = last 24h filter

# Login (optional). If not provided or fails, searches still work anonymously.
LOGIN_ENABLED = True
BUILTIN_EMAIL = os.getenv("BUILTIN_EMAIL")
BUILTIN_PASSWORD = os.getenv("BUILTIN_PASSWORD")

# Playwright browser options
HEADLESS = False  # start visible for debugging; set True when you confirm it works
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

# SMTP / Email
SMTP = {
    "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "user": os.getenv("SMTP_USER"),
    "pass": os.getenv("SMTP_PASS"),
    "to":   os.getenv("MAIL_TO") or os.getenv("EMAIL_TO"),
    "from": os.getenv("MAIL_FROM") or os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER"),
}


# =============== Data Model ===============

@dataclass(frozen=True)
class Job:
    title: str
    company: str
    location: str
    posted: str
    url: str
    matched_on: str  # keyword or "(URL)"


# =============== URL Builders ===============

def build_search_url_from_keyword(keyword: str) -> str:
    """
    Build: https://builtin.com/jobs?search=<kw>&country=USA&allLocations=true
    plus per_page/sort/status and optional daysSinceUpdated/remote_only.
    """
    base = "https://builtin.com/jobs"
    params = {
        "search": keyword,
        "country": "USA",
        "allLocations": "true",
    #     "per_page": str(MAX_PER_SEARCH),
    #     "sort": "recent",
    #     "status": "all",
    }
    if REMOTE_ONLY:
        params["remote_only"] = "true"
    if DAYS_SINCE_UPDATED is not None:
        params["daysSinceUpdated"] = str(DAYS_SINCE_UPDATED)
    return f"{base}?{urlencode(params, quote_via=quote_plus)}"


def normalize_search_url(url: str) -> str:
    """
    Ensure the provided URL includes per_page/sort/status and optional
    daysSinceUpdated/remote_only without breaking your other params.
    """
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))

    q.setdefault("country", "USA")
    q.setdefault("allLocations", "true")
    # q.setdefault("per_page", str(MAX_PER_SEARCH))
    # q.setdefault("sort", "recent")
    # q.setdefault("status", "all")
    if REMOTE_ONLY:
        q["remote_only"] = "true"
    if DAYS_SINCE_UPDATED is not None:
        q["daysSinceUpdated"] = str(DAYS_SINCE_UPDATED)

    new_q = urlencode(q, doseq=True, quote_via=quote_plus)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_q, parsed.fragment))


# =============== Helpers ===============

def looks_recent(posted_text: str) -> bool:
    """
    Heuristic on 'posted' text: 'today', 'x hours ago', '3 days ago', '1 week ago', etc.
    """
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
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP["from"]
    msg["To"] = SMTP["to"]
    msg.attach(MIMEText(html_body, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP["host"], SMTP["port"]) as server:
        server.starttls(context=ctx)
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
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
        f"<p>Total: {len(jobs)}</p>"
    )


# =============== Playwright Scraper ===============

def builtin_login(page) -> None:
    """
    Optional login. If it fails or credentials not set, continue anonymously.
    """
    if not LOGIN_ENABLED or not (BUILTIN_EMAIL and BUILTIN_PASSWORD):
        print("Login skipped.")
        return
    print("Logging in to Built In...")
    page.goto("https://builtin.com/users/sign_in", timeout=60000)
    try:
        page.wait_for_selector('input[name="email"]', timeout=30000)
        page.fill('input[name="email"]', BUILTIN_EMAIL)
        page.fill('input[name="password"]', BUILTIN_PASSWORD)
        page.click('button[type="submit"]')
        try:
            page.wait_for_selector('a[href^="/profile"]', timeout=30000)
        except PWTimeout:
            time.sleep(2)
        print("Login flow finished (continuing).")
    except Exception as e:
        print(f"Login warning: {e}. Continuing without login.")


def parse_jobs_on_page(page, matched_on: str) -> List[Job]:
    """
    Robust link-first strategy:
      1) Wait/scroll to trigger lazy load
      2) Collect all '/jobs/...' links on the result page
      3) Return lightweight Job objects with URL (details filled later)
    """
    results: List[Job] = []

    # Trigger content load & lazy lists
    page.wait_for_timeout(1000)
    for _ in range(3):
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(600)

    link_nodes = page.query_selector_all('a[href^="/jobs/"]:not([href*="companies"])')
    seen = set()
    for a in link_nodes:
        href = (a.get_attribute("href") or "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://builtin.com" + href
        if href in seen:
            continue
        seen.add(href)

        title = (a.inner_text() or "").strip()
        results.append(Job(
            title=title or "",
            company="",
            location="",
            posted="",
            url=href,
            matched_on=matched_on,
        ))

    return results


def hydrate_from_detail(context, jobs: List[Job]) -> List[Job]:
    """
    Visit each job detail page to extract title/company/location/posted reliably.
    Avoids brittle selectors on the search page.
    """
    hydrated: List[Job] = []
    for j in jobs[:MAX_PER_SEARCH]:
        page = context.new_page()
        try:
            page.goto(j.url, timeout=60000)
            page.wait_for_selector("h1, h2, title", timeout=15000)

            # Title (try h1/h2 first, then og:title)
            title_el = page.query_selector("h1") or page.query_selector("h2")
            title = (title_el.inner_text().strip() if title_el else "")
            if not title:
                meta_title = page.query_selector('meta[property="og:title"]')
                if meta_title:
                    title = (meta_title.get_attribute("content") or "").strip()

            # Company
            company_el = (
                page.query_selector('a[href^="/company/"]')
                or page.query_selector('[data-qa="company"]')
                or page.query_selector('[class*="company"]')
            )
            company = (company_el.inner_text().strip() if company_el else "")

            # Posted/Updated
            posted_el = (
                page.query_selector("time")
                or page.query_selector('[class*="posted"]')
                or page.query_selector('[data-qa="posted"]')
            )
            posted = (posted_el.inner_text().strip() if posted_el else "")

            # Location
            loc_el = (
                page.query_selector('[data-qa="location"]')
                or page.query_selector('[class*="location"]')
                or page.query_selector('a[href*="locations"]')
            )
            location = (loc_el.inner_text().strip() if loc_el else "")

            if POSTED_WITHIN_DAYS and posted and not looks_recent(posted):
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


def build_targets() -> List[Tuple[str, str]]:
    """
    Returns a list of (url, label). Label is "(URL)" or the keyword.
    """
    targets: List[Tuple[str, str]] = []
    if SEARCH_URLS:
        for u in SEARCH_URLS:
            targets.append((normalize_search_url(u), "(URL)"))
    else:
        for kw in KEYWORDS:
            targets.append((build_search_url_from_keyword(kw), kw))
    return targets


def sort_key(j: Job) -> Tuple[int, str]:
    """
    Sort by "newest-looking" based on posted text, then title.
    """
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
            try:
                page.wait_for_selector("a[href*='/jobs/'], article", timeout=15000)
            except PWTimeout:
                pass

            # Try parse once; if empty, scroll a bit more and retry
            found = parse_jobs_on_page(page, matched_on=label)
            if not found:
                for _ in range(2):
                    page.mouse.wheel(0, 25000)
                    page.wait_for_timeout(800)
                found = parse_jobs_on_page(page, matched_on=label)

            # Hydrate details from job detail pages
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
