"""
Unity Job Watcher
------------------
Polls public, no-auth ATS APIs (Greenhouse / Lever) for job postings at
companies you configure, filters for keywords (default: "unity"), and
sends NEW matches to a Telegram chat via a bot.

Run it on a schedule (Windows Task Scheduler / cron). Each run:
  1. Fetches current job listings for every company in COMPANIES.
  2. Filters by KEYWORDS (case-insensitive, matched against job title).
  3. Compares against seen_jobs.json (local memory of what you've
     already been notified about).
  4. Sends only the NEW matches to Telegram.
  5. Updates seen_jobs.json so you don't get duplicate notifications.

This only talks to ATS endpoints (Greenhouse, Lever) that are public
JSON APIs by design - no LinkedIn scraping, no ToS issues, no
anti-bot evasion needed.
"""

import html
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from config import (
    COMPANIES,
    MAX_AGE_DAYS,
    MAX_GAP_BETWEEN_ALL_AND_ANY,
    REQUIRE_ALL,
    REQUIRE_ANY,
    SEEN_FILE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_CHANNELS,
)

# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "unity-job-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_iso_datetime(s):
    """Parse an ISO-8601 timestamp (with timezone offset) into an aware datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def fetch_greenhouse_jobs(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    data = http_get_json(url)
    jobs = []
    for j in data.get("jobs", []):
        posted_at = parse_iso_datetime(j.get("updated_at"))
        jobs.append(
            {
                "id": f"greenhouse:{token}:{j.get('id')}",
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
                "posted_at": posted_at,
            }
        )
    return jobs


def fetch_lever_jobs(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = http_get_json(url)
    jobs = []
    for j in data:
        created_at_ms = j.get("createdAt")
        posted_at = (
            datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
            if created_at_ms
            else None
        )
        jobs.append(
            {
                "id": f"lever:{token}:{j.get('id')}",
                "title": j.get("text", ""),
                "location": (j.get("categories") or {}).get("location", ""),
                "url": j.get("hostedUrl", ""),
                "posted_at": posted_at,
            }
        )
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
}


def strip_html(raw_html):
    """Turn a snippet of Telegram message HTML into plain text."""
    s = re.sub(r"<br\s*/?>", "\n", raw_html)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return s.strip()


def fetch_telegram_channel_posts(channel, timeout=15):
    """
    Scrape the public, no-login preview page Telegram serves at
    https://t.me/s/<channel> for a public channel's recent posts.
    No bot token, no auth, no API call - this is the same page anyone
    gets if they visit that URL in a browser without logging in.
    """
    url = f"https://t.me/s/{channel}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    posts = []
    for m in re.finditer(r'data-post="([^"]+)"', body):
        post_id = m.group(1)  # e.g. "ingamejob_dev/1234"
        window = body[m.end() : m.end() + 20000]
        text_match = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', window, re.DOTALL
        )
        if not text_match:
            continue
        text = strip_html(text_match.group(1))
        if not text:
            continue

        time_match = re.search(r'<time datetime="([^"]+)"', window)
        posted_at = parse_iso_datetime(time_match.group(1)) if time_match else None

        posts.append(
            {
                "id": f"tg:{post_id}",
                "title": text[:200],  # used for display only
                "match_text": text,  # full text, used for keyword matching
                "location": "",
                "url": f"https://t.me/{post_id}",
                "posted_at": posted_at,
            }
        )
    return posts

# ---------------------------------------------------------------------------
# Filtering / state
# ---------------------------------------------------------------------------


def is_recent_enough(posted_at):
    """True if posted_at is within MAX_AGE_DAYS, or if posted_at/MAX_AGE_DAYS is unknown/disabled."""
    if MAX_AGE_DAYS is None or posted_at is None:
        return True  # no date info available - don't filter it out, just can't judge age
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    return posted_at >= cutoff
    
def matches_keywords(text):
    # ALL of REQUIRE_ALL must appear somewhere in the text (not necessarily
    # adjacent to each other). Each word is blocked from matching as part of
    # a longer word (e.g. "unity" won't match inside "community" or
    # "opportunity").
    all_matches = []  # list of match-lists, one per REQUIRE_ALL word
    for word in REQUIRE_ALL:
        found = list(re.finditer(rf"(?<![a-zA-Z]){re.escape(word)}", text, re.IGNORECASE))
        if not found:
            return False
        all_matches.append(found)
 
    if not REQUIRE_ANY:
        return True
 
    any_matches = []
    for word in REQUIRE_ANY:
        any_matches.extend(
            re.finditer(rf"(?<![a-zA-Z]){re.escape(word)}", text, re.IGNORECASE)
        )
    if not any_matches:
        return False
 
    # On top of all REQUIRE_ALL words appearing somewhere, at least one
    # REQUIRE_ALL match must come BEFORE at least one REQUIRE_ANY match,
    # with no more than MAX_GAP_BETWEEN_ALL_AND_ANY characters between the
    # end of the ALL match and the start of the ANY match.
    for word_matches in all_matches:
        for all_m in word_matches:
            for any_m in any_matches:
                gap = any_m.start() - all_m.end()
                if 0 <= gap <= MAX_GAP_BETWEEN_ALL_AND_ANY:
                    return True
 
    return False
 


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_ids), f, indent=2)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram not configured, printing instead:\n" + text)
        return True  # treat as "delivered" (to console) so seen-state still advances
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        print(f"[ERROR] Telegram send failed: {e.read().decode('utf-8')}")
        return False
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    seen = load_seen()
    new_matches = []

    for company in COMPANIES:
        fetcher = FETCHERS.get(company["ats"])
        if not fetcher:
            print(f"[WARN] Unknown ATS '{company['ats']}' for {company['name']}, skipping.")
            continue
        try:
            jobs = fetcher(company["token"])
        except Exception as e:
            print(f"[ERROR] Failed to fetch {company['name']}: {e}")
            continue

        for job in jobs:
            if job["id"] in seen:
                continue
            if not matches_keywords(job.get("match_text", job["title"])):
                continue
            if not is_recent_enough(job.get("posted_at")):
                continue
            new_matches.append({**job, "company": company["name"]})
            seen.add(job["id"])

    for channel in TELEGRAM_CHANNELS:
        try:
            posts = fetch_telegram_channel_posts(channel)
        except Exception as e:
            print(f"[ERROR] Failed to fetch Telegram channel @{channel}: {e}")
            continue

        for post in posts:
            if post["id"] in seen:
                continue
            if not matches_keywords(post.get("match_text", post["title"])):
                continue
            if not is_recent_enough(post.get("posted_at")):
                continue
            new_matches.append({**post, "company": f"@{channel}"})
            seen.add(post["id"])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if new_matches:
        lines = [f"<b>{len(new_matches)} new Unity job(s) found</b> — {now}"]
        separator = "-" * 60
        for m in new_matches:
            loc = f" - {m['location']}" if m["location"] else ""
            lines.append(f"\n{separator}\n<b>{m['company']}</b>: {m['title']}{loc}\n{m['url']}")
        delivered = send_telegram_message("\n".join(lines))
        if delivered:
            print(f"Sent {len(new_matches)} new match(es).")
        else:
            # Roll back: don't mark these as seen, so they're retried next run.
            for m in new_matches:
                seen.discard(m["id"])
            print(
                f"[ERROR] Delivery failed - {len(new_matches)} match(es) NOT marked as seen, will retry next run."
            )
    else:
        print("No new matches this run.")

    save_seen(seen)


if __name__ == "__main__":
    sys.exit(main() or 0)