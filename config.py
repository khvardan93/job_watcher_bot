import os

COMPANIES = [
    {"name": "Cloud Chamber", "ats": "greenhouse", "token": "cloudchamberen"},
    {"name": "Scopely", "ats": "greenhouse", "token": "scopely"},
    {"name": "Voodoo", "ats": "lever", "token": "voodoo"},
    # {"name": "Niantic", "ats": "greenhouse", "token": "niantic"},  # returns 404 live - token may have changed, verify manually before re-adding
]

REQUIRE_ALL = ["unity"]
REQUIRE_ANY = ["engineer", "developer", "разработчик"]  # at least ONE must appear
MAX_GAP_BETWEEN_ALL_AND_ANY = 50

# Only notify about postings dated within this many days. Set to None to disable.
MAX_AGE_DAYS = 7

# Public Telegram channels to scan (no auth needed - uses t.me/s/<channel>).
TELEGRAM_CHANNELS = [
    "ingamejob_dev",
    "devjobs",
    "unityjobs_pub",
    "forgamedev",
    "bestjobinarmenia",
    "gamedevjob",
    "unity_jobs",
    "itdigitaldevhunt",
    "jobs_poland_peopleup",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_jobs.json")
