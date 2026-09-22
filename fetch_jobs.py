"""
Pulls job listings from public, ToS-friendly sources:
  - RemoteOK (public JSON API)
  - We Work Remotely (public RSS feeds)
  - Hacker News "Who is hiring?" monthly thread (public Algolia API)
  - Any Greenhouse or Lever company job board (public JSON endpoints)

No scraping of sites that prohibit it (LinkedIn, Indeed) -- those need a
different approach and are left out on purpose.

Only listings posted within MAX_AGE_DAYS are kept -- older postings are
more likely to already be filled or getting buried under later applicants,
so freshness matters more here than raw listing count.
"""

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

HEADERS = {"User-Agent": "job-scorer/1.0 (personal use)"}
MAX_AGE_DAYS = 7


def get_json(url, **kwargs):
    """requests' .json() mis-decodes RemoteOK/Lever's UTF-8 body as latin-1
    when the Content-Type header omits a charset. Decode explicitly instead."""
    resp = requests.get(url, headers=HEADERS, timeout=kwargs.pop("timeout", 10), **kwargs)
    resp.raise_for_status()
    return json.loads(resp.content.decode("utf-8"))


def fix_mojibake(text):
    """Some sources (RemoteOK in particular) store text that was already
    UTF-8-decoded-as-latin-1 once before saving. Reverse that if present;
    leave anything else untouched."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = fix_mojibake(text)
    return re.sub(r"\s+", " ", text).strip()


def is_recent(posted_at, max_age_days=MAX_AGE_DAYS):
    """True if posted_at is a tz-aware datetime within the last max_age_days.
    Unknown/unparseable dates are excluded, not assumed recent -- a listing
    with no verifiable date isn't one we can promise is a week old."""
    if posted_at is None:
        return False
    return posted_at >= datetime.now(timezone.utc) - timedelta(days=max_age_days)


def fetch_remoteok(limit=15, max_age_days=MAX_AGE_DAYS):
    try:
        jobs = get_json("https://remoteok.com/api")
    except Exception:
        return []

    listings = []
    for job in jobs:
        if not isinstance(job, dict) or "position" not in job:
            continue  # first item is a legal-notice object, not a job

        try:
            posted_at = datetime.fromisoformat(job.get("date", ""))
        except ValueError:
            posted_at = None
        if not is_recent(posted_at, max_age_days):
            continue

        position = fix_mojibake(job.get("position", "Untitled"))
        company = fix_mojibake(job.get("company", "Unknown company"))
        listings.append(
            {
                "title": f"{position} at {company}",
                "description": strip_html(job.get("description", ""))[:1200],
                "url": job.get("url", ""),
                "location": fix_mojibake(job.get("location") or "") or "Remote",
                "source": "RemoteOK",
                "posted_at": posted_at,
            }
        )
        if len(listings) >= limit:
            break
    return listings


def fetch_wwr(categories=("remote-programming-jobs",), limit=15, max_age_days=MAX_AGE_DAYS):
    listings = []
    for category in categories:
        try:
            resp = requests.get(f"https://weworkremotely.com/categories/{category}.rss", headers=HEADERS, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception:
            continue

        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue

            try:
                posted_at = parsedate_to_datetime(item.findtext("pubDate") or "")
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                posted_at = None
            if not is_recent(posted_at, max_age_days):
                continue

            description = strip_html(item.findtext("description") or "")[:1200]
            link = (item.findtext("link") or "").strip()
            region = (item.findtext("region") or "").strip() or "Remote"
            listings.append(
                {
                    "title": title,
                    "description": description,
                    "url": link,
                    "location": region,
                    "source": "WeWorkRemotely",
                    "posted_at": posted_at,
                }
            )
            if len(listings) >= limit:
                break
    return listings


def fetch_hn_whos_hiring(limit=20, max_age_days=MAX_AGE_DAYS):
    try:
        search = get_json(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"tags": "story", "query": "Who is hiring"},
        )
        hits = search.get("hits", [])
        thread_id = next((h["objectID"] for h in hits if h.get("title", "").lower().startswith("ask hn: who is hiring")), None)
        if not thread_id:
            return []

        item = get_json(f"https://hn.algolia.com/api/v1/items/{thread_id}", timeout=15)
        comments = item.get("children", [])
    except Exception:
        return []

    listings = []
    for comment in comments:
        text = strip_html(comment.get("text") or "")
        if not text or len(text) < 40:
            continue

        created_at_i = comment.get("created_at_i")
        posted_at = datetime.fromtimestamp(created_at_i, tz=timezone.utc) if created_at_i else None
        if not is_recent(posted_at, max_age_days):
            continue

        title = text[:70] + ("..." if len(text) > 70 else "")
        listings.append(
            {
                "title": f"HN posting: {title}",
                "description": text[:1500],
                "url": f"https://news.ycombinator.com/item?id={comment.get('id', '')}",
                "location": "Not specified",
                "source": "HN Who is Hiring",
                "posted_at": posted_at,
            }
        )
        if len(listings) >= limit:
            break
    return listings


def fetch_greenhouse(company, limit=15, max_age_days=MAX_AGE_DAYS):
    try:
        jobs = get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs",
            params={"content": "true"},
        ).get("jobs", [])
    except Exception:
        return []

    listings = []
    for job in jobs:
        raw_date = job.get("first_published") or job.get("updated_at")
        try:
            posted_at = datetime.fromisoformat(raw_date) if raw_date else None
        except ValueError:
            posted_at = None
        if not is_recent(posted_at, max_age_days):
            continue

        listings.append(
            {
                "title": f"{job.get('title', 'Untitled')} at {company}",
                "description": strip_html(job.get("content", ""))[:1200],
                "url": job.get("absolute_url", ""),
                "location": (job.get("location") or {}).get("name") or "Not specified",
                "source": f"Greenhouse ({company})",
                "posted_at": posted_at,
            }
        )
        if len(listings) >= limit:
            break
    return listings


def fetch_lever(company, limit=15, max_age_days=MAX_AGE_DAYS):
    try:
        jobs = get_json(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    except Exception:
        return []

    listings = []
    for job in jobs:
        created_at_ms = job.get("createdAt")
        posted_at = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc) if created_at_ms else None
        if not is_recent(posted_at, max_age_days):
            continue

        description = job.get("descriptionPlain") or strip_html(job.get("description", ""))
        listings.append(
            {
                "title": f"{job.get('text', 'Untitled')} at {company}",
                "description": description[:1200],
                "url": job.get("hostedUrl", ""),
                "location": (job.get("categories") or {}).get("location") or "Not specified",
                "source": f"Lever ({company})",
                "posted_at": posted_at,
            }
        )
        if len(listings) >= limit:
            break
    return listings


def fetch_remotive(limit=15, max_age_days=MAX_AGE_DAYS):
    """Remotive's free API is personal-use only per their terms: max ~4
    requests/day, listings delayed 24h, no redistributing the data
    elsewhere. Fine for this tool's own display; don't hammer it."""
    try:
        jobs = get_json("https://remotive.com/api/remote-jobs").get("jobs", [])
    except Exception:
        return []

    listings = []
    for job in jobs:
        raw_date = job.get("publication_date")
        try:
            posted_at = datetime.fromisoformat(raw_date) if raw_date else None
            if posted_at and posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
        except ValueError:
            posted_at = None
        if not is_recent(posted_at, max_age_days):
            continue

        listings.append(
            {
                "title": f"{job.get('title', 'Untitled')} at {job.get('company_name', 'Unknown company')}",
                "description": strip_html(job.get("description", ""))[:1200],
                "url": job.get("url", ""),
                "location": job.get("candidate_required_location") or "Remote",
                "source": "Remotive",
                "posted_at": posted_at,
            }
        )
        if len(listings) >= limit:
            break
    return listings


def fetch_arbeitnow(limit=15, max_age_days=MAX_AGE_DAYS):
    try:
        jobs = get_json("https://www.arbeitnow.com/api/job-board-api").get("data", [])
    except Exception:
        return []

    listings = []
    for job in jobs:
        created = job.get("created_at")
        posted_at = datetime.fromtimestamp(created, tz=timezone.utc) if created else None
        if not is_recent(posted_at, max_age_days):
            continue

        location = job.get("location") or ("Remote" if job.get("remote") else "Not specified")
        listings.append(
            {
                "title": f"{job.get('title', 'Untitled')} at {job.get('company_name', 'Unknown company')}",
                "description": strip_html(job.get("description", ""))[:1200],
                "url": job.get("url", ""),
                "location": location,
                "source": "Arbeitnow",
                "posted_at": posted_at,
            }
        )
        if len(listings) >= limit:
            break
    return listings


def fetch_jobicy(limit=15, max_age_days=MAX_AGE_DAYS):
    try:
        jobs = get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 50}).get("jobs", [])
    except Exception:
        return []

    listings = []
    for job in jobs:
        raw_date = job.get("pubDate")
        try:
            posted_at = datetime.fromisoformat(raw_date) if raw_date else None
        except ValueError:
            posted_at = None
        if not is_recent(posted_at, max_age_days):
            continue

        listings.append(
            {
                "title": f"{job.get('jobTitle', 'Untitled')} at {job.get('companyName', 'Unknown company')}",
                "description": strip_html(job.get("jobExcerpt") or job.get("jobDescription", ""))[:1200],
                "url": job.get("url", ""),
                "location": job.get("jobGeo") or "Remote",
                "source": "Jobicy",
                "posted_at": posted_at,
            }
        )
        if len(listings) >= limit:
            break
    return listings


def fetch_all(sources, greenhouse_companies=None, lever_companies=None, limit_per_source=15, max_age_days=MAX_AGE_DAYS):
    listings = []
    if "remoteok" in sources:
        listings += fetch_remoteok(limit_per_source, max_age_days)
    if "wwr" in sources:
        listings += fetch_wwr(limit=limit_per_source, max_age_days=max_age_days)
    if "hn" in sources:
        listings += fetch_hn_whos_hiring(limit_per_source, max_age_days)
    if "remotive" in sources:
        listings += fetch_remotive(limit_per_source, max_age_days)
    if "arbeitnow" in sources:
        listings += fetch_arbeitnow(limit_per_source, max_age_days)
    if "jobicy" in sources:
        listings += fetch_jobicy(limit_per_source, max_age_days)
    for company in greenhouse_companies or []:
        listings += fetch_greenhouse(company.strip(), limit_per_source, max_age_days)
    for company in lever_companies or []:
        listings += fetch_lever(company.strip(), limit_per_source, max_age_days)
    return listings
