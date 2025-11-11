import json
import os
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv
from email.utils import formataddr
import time
import random
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright  # ✅ Playwright for browser fallback

load_dotenv()

API_URL = "https://hiring.cafe/api/search-jobs"
STATE_FILE = "state.json"

SEARCH_KEYWORDS = [
    ".NET Developer",
    ".NET Core Developer",
    "ASP.NET Developer",
    "Full Stack .NET",
    "Full-Stack .NET Developer",
    "Backend .NET Developer",
    "AWS .NET Developer",
    "React.js Developer",
    "ReactJS Developer",
    "Full-Stack Developer (React/.NET)",
    "JavaScript Developer",
    "Frontend Engineer (JavaScript)",
    "C# Developer",
    "Azure Developer",
    "Front End Developer"
]

BASE_PAYLOAD = {
    "size": 40,
    "page": 0,
    "searchState": {
        "locations": [{
            "id": "FxY1yZQBoEtHp_8UEq7V",
            "types": ["country"],
            "address_components": [{
                "long_name": "United States",
                "short_name": "US",
                "types": ["country"]
            }],
            "formatted_address": "United States",
            "population": 327167434,
            "workplace_types": [],
            "options": {"flexible_regions": ["anywhere_in_continent", "anywhere_in_world"]}
        }],
        "applicationFormEase": [],
        "workplaceTypes": ["Remote", "Hybrid", "Onsite"],
        "commitmentTypes": ["Full Time", "Contract"],
        "seniorityLevel": ["No Prior Experience Required", "Entry Level", "Mid Level"],
        "roleTypes": ["Individual Contributor"],
        "roleYoeRange": [0, 4],
        "dateFetchedPastNDays": 2,
         # "sortBy": "default"
        "sortBy": "date"
    }
}

# ---------- STATE MANAGEMENT ----------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_state(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)

# ---------- EMAIL UTILITY ----------
def send_email(new_jobs):
    if not new_jobs:
        print("No new jobs found. Email not sent.")
        return

    table_rows = "".join(
        f"<tr>"
        f"<td>{job['title']}</td>"
        f"<td>{job['company']}</td>"
        f"<td>{job['location']}</td>"
        f"<td>{job['salary']}</td>"
        f"<td><a href='{job['url']}'>Apply</a></td>"
        f"<td>{job['searchKey']}</td>"
        f"</tr>"
        for job in new_jobs
    )

    html_content = f"""
    <html>
      <body>
        <h3>New HiringCafe Job Listings ({datetime.now().strftime('%Y-%m-%d %H:%M')})</h3>
        <table border="1" cellspacing="0" cellpadding="6"
               style="border-collapse: collapse; font-family: Arial, sans-serif; font-size: 13px;">
          <thead>
            <tr style="background-color:#f2f2f2;">
              <th>Job Title</th>
              <th>Company</th>
              <th>Location</th>
              <th>Salary</th>
              <th>Apply Link</th>
              <th>Search Key</th>
            </tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>
        <br><i>Total new jobs: {len(new_jobs)}</i>
      </body>
    </html>
    """

    msg = MIMEText(html_content, "html", "utf-8")
    subject_prefix = os.getenv("EMAIL_SUBJECT_PREFIX", "[HiringCafe]")
    from_name = os.getenv("EMAIL_FROM_NAME", "HiringCafe Job Watcher")
    mail_from = os.getenv("MAIL_FROM", os.getenv("SMTP_USER"))
    mail_to = os.getenv("MAIL_TO", mail_from)

    if new_jobs:
        subject = f"{subject_prefix} {len(new_jobs)} matching role(s) • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    else:
        subject = "[TEST] HiringCafe Job Watcher SMTP OK"

    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, mail_from))
    msg["To"] = mail_to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
        server.send_message(msg)

    print(f"✅ Email sent with {len(new_jobs)} new jobs.")

# ---------- PLAYWRIGHT FALLBACK ----------
def playwright_fetch_jobs(api_url, payload):
    """Fetch jobs using a real browser to bypass 401/403/429 in GitHub Actions."""
    print("🌐 Using Playwright browser to fetch jobs...")
    from json import JSONDecodeError
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        response = page.request.post(
            api_url,
            data=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Origin": "https://hiring.cafe",
                "Referer": "https://hiring.cafe/"
            },
            timeout=45000
        )

        # Safely handle possible HTML / empty body
        text = response.text()
        try:
            data = response.json()
        except JSONDecodeError:
            print(f"⚠️ Playwright got non-JSON response (HTTP {response.status}) — likely rate-limited.")
            print("Response snippet:", text[:200])
            data = {"results": []}

        browser.close()
        return data

# ---------- FETCH FROM API ----------
def fetch_jobs_for_keyword(keyword):
    payload = BASE_PAYLOAD.copy()
    payload["searchState"] = dict(BASE_PAYLOAD["searchState"])
    payload["searchState"]["searchQuery"] = keyword

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://hiring.cafe/",
        "Origin": "https://hiring.cafe"
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=45)
        if response.status_code == 200:
            data = response.json()
        elif response.status_code in [401, 403]:
            data = playwright_fetch_jobs(API_URL, payload)
        else:
            print(f"⚠️ Unexpected HTTP {response.status_code}, retrying with Playwright...")
            data = playwright_fetch_jobs(API_URL, payload)
    except Exception as e:
        print(f"❌ Error with requests: {e}, switching to Playwright...")
        data = playwright_fetch_jobs(API_URL, payload)

    time.sleep(random.randint(8, 15))
    jobs = []
    for j in data.get("results", []):
        job_id = j.get("id")
        info = j.get("job_information", {})
        job_data = j.get("v5_processed_job_data", {})
        company_data = j.get("v5_processed_company_data", {})

        title = job_data.get("core_job_title") or info.get("title") or "Untitled"
        company = company_data.get("name") or job_data.get("company_name") or "Unknown"
        apply_url = j.get("apply_url") or f"https://hiring.cafe/job/{job_id}"
        location = job_data.get("formatted_workplace_location", "N/A")
        salary_min = job_data.get("yearly_min_compensation")
        salary_max = job_data.get("yearly_max_compensation")
        salary_text = (
            f"${salary_min:,.0f} - ${salary_max:,.0f}"
            if salary_min and salary_max else "Not Disclosed"
        )

        jobs.append({
            "id": job_id,
            "title": title,
            "company": company,
            "url": apply_url,
            "location": location,
            "salary": salary_text,
            "searchKey": keyword
        })
    print(f"✅ {len(jobs)} jobs fetched successfully for {keyword}")
    return jobs

def deduplicate_jobs(jobs):
    seen = set()
    unique = []
    for job in jobs:
        key = job.get("id") or f"{job['company']}|{job['title']}|{job['location']}"
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique

# ---------- MAIN ----------
def main():
    seen = load_state()
    all_new_jobs = []
    current_ids = set()

    for keyword in SEARCH_KEYWORDS:
        print(f"🔍 Searching for {keyword}...")
        jobs = fetch_jobs_for_keyword(keyword)
        for job in jobs:
            current_ids.add(job["id"])
            if job["id"] not in seen:
                all_new_jobs.append(job)

    all_new_jobs = deduplicate_jobs(all_new_jobs)
    backend_url = "https://jobwatch-api-g6a3cjenesbna5gv.canadacentral-01.azurewebsites.net/api/applications"
    jobs_backend = [{
        "job_id": jb["id"],
        "job_title": jb["title"],
        "company": jb["company"],
        "location": jb["location"],
        "salary": jb["salary"],
        "description": "None",
        "apply_link": jb["url"],
        "search_key": jb["searchKey"],
        "posted_time": datetime.now(timezone.utc).isoformat(),
        "source": "HiringCafe",
        "matching_score": 0.0
    } for jb in all_new_jobs]

    response = requests.post(backend_url, json=jobs_backend, verify=False)
    print("Ingested jobs:", response.text or "(no content)")

    if all_new_jobs:
        send_email(all_new_jobs)
        seen |= current_ids
        save_state(seen)
    else:
        print("No new jobs found this cycle.")

if __name__ == "__main__":
    main()
