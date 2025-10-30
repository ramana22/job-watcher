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

load_dotenv()

SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")
API_URL = "https://hiring.cafe/api/search-jobs"
PROXY_URL = f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPINGBEE_API_KEY}&url="

STATE_FILE = "state.json"

# One combined search (reduces API hits from 4 → 1)
SEARCH_KEYWORDS = [
    ".NET Developer",
    "Full Stack .NET",
    "C# Developer",
    "Azure Developer",
]


# ---------- PAYLOAD TEMPLATE ----------
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
         # "Simple" or "Time Consuming"
        "applicationFormEase": [],
        "workplaceTypes": ["Remote", "Hybrid", "Onsite"],
        "commitmentTypes": ["Full Time", "Contract"],
        "seniorityLevel": ["No Prior Experience Required", "Entry Level", "Mid Level"],
        "roleTypes": ["Individual Contributor"],
        "roleYoeRange": [0, 4],
        "dateFetchedPastNDays": 2,
        "sortBy": "default"
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
          <tbody>
            {table_rows}
          </tbody>
        </table>
        <br><i>Total new jobs: {len(new_jobs)}</i>
      </body>
    </html>
    """

    msg = MIMEText(html_content, "html", "utf-8")


    # ---- Configurable Email Settings ----
    subject_prefix = os.getenv("EMAIL_SUBJECT_PREFIX", "[HiringCafe]")
    from_name = os.getenv("EMAIL_FROM_NAME", "HiringCafe Job Watcher")
    mail_from = os.getenv("MAIL_FROM", os.getenv("SMTP_USER"))
    mail_to = os.getenv("MAIL_TO", mail_from)

    # ---- Build Subject ----
    if new_jobs:
        subject = f"{subject_prefix} {len(new_jobs)} matching role(s) • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    else:
        subject = "[TEST] HiringCafe Job Watcher SMTP OK"

    # ---- Construct Email ----
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, mail_from))
    msg["To"] = mail_to


    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
        server.send_message(msg)

    print(f"✅ Email sent with {len(new_jobs)} new jobs.")

# ---------- FETCH FROM API ----------
def fetch_jobs_for_keyword(keyword):
    payload = BASE_PAYLOAD.copy()
    payload["searchState"] = dict(BASE_PAYLOAD["searchState"])
    payload["searchState"]["searchQuery"] = keyword

    # Full browser headers to bypass cloud firewalls
    headers = {
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://hiring.cafe/",
        "Origin": "https://hiring.cafe",
        "Connection": "keep-alive"
    }

    proxies = {}
    if os.getenv("HTTP_PROXY"):
        proxies = {"https": os.getenv("HTTP_PROXY")}

    max_retries = 5
    wait = 10

    for attempt in range(max_retries):
        try:
            target_url = f"{PROXY_URL}{API_URL}"
            response = requests.post(target_url, json=payload, headers=headers, timeout=45)
            if response.status_code == 429:
                print(f"⚠️ Rate limited, waiting {wait}s (attempt {attempt+1}/{max_retries})...")
                time.sleep(wait + random.randint(5, 10))
                wait *= 2
                continue
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                data = json.loads(response.text)
            break
        except Exception as e:
            print(f"❌ Error fetching jobs: {e}")
            if attempt < max_retries - 1:
                time.sleep(wait)
                wait *= 2
            else:
                return []
    else:
        print("🚫 Skipping fetch after repeated errors.")
        return []

    # Random sleep to mimic human delay
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
            if salary_min and salary_max
            else "Not Disclosed"
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



    print(f"✅ {len(jobs)} jobs fetched successfully.")
    return jobs

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

    if all_new_jobs:
        send_email(all_new_jobs)
        seen |= current_ids
        save_state(seen)
    else:
        print("No new jobs found this cycle.")

if __name__ == "__main__":
    main()
