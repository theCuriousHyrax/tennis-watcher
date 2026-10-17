#!/usr/bin/env python3
"""
Tennis court watcher: polls LTA court availability and sends a Telegram
message whenever a slot that matches your rules newly appears.

Data source: the LTA's public court-availability endpoint -- the same one
behind https://www.lta.org.uk/play/book-a-tennis-court. No API key needed.
Stdlib only: runs as a plain cron job or in GitHub Actions with zero installs.

Your rules (encoded below, all configurable):
  - Weekends: any time.
  - Every other Friday (anchor 2026-10-09): any time.
  - Other weekdays: before 09:00 or from 17:00.
  - Notify only; never auto-book. You get a one-tap booking link per alert.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ------------------------------ configuration ------------------------------

LONDON = ZoneInfo("Europe/London")

# Venues to watch. The id is the GUID at the end of the venue's page URL at
# https://www.lta.org.uk/play/book-a-tennis-court/courts/<slug>_<id>/
# To add another Hackney court (Clissold Park, Hackney Downs, Millfields...),
# search the venue on lta.org.uk, copy the GUID from its page URL, add an entry.
VENUES = [
    {
        "name": "London Fields Park",
        "id": "c67a5893-b53e-4b77-8013-95fcfbe1526a",
        "book_url": "https://clubspark.lta.org.uk/LondonFieldsPark/Booking/BookByDate#?date={date}",
    },
    {
        "name": "Joe White Gardens",
        "id": "6bc2df28-20fd-4bed-a6e6-e6ed2cbf1c5a",
        "book_url": "https://clubspark.lta.org.uk/AskeGardens/Booking/BookByDate#?date={date}",
    },
    {
        "name": "Hackney Downs",
        "id": "3df475ea-0986-403a-9f64-e520e234c73a",
        "book_url": "https://clubspark.lta.org.uk/HackneyDowns/Booking/BookByDate#?date={date}",
    },
    {
        "name": "Clissold Park",
        "id": "24e84e6c-f529-4cd1-8793-cb0cc106e6e7",
        "book_url": "https://clubspark.lta.org.uk/ClissoldParkHackney/Booking/BookByDate#?date={date}",
    },
]

DAYS_AHEAD = 7                    # Hackney opens bookings ~7 days ahead
EARLY_CUTOFF = time(9, 0)         # weekday slots starting before this match
EVENING_FROM = time(17, 0)        # weekday slots at/after this match
ON_FRIDAY_ANCHOR = date(2026, 10, 9)  # most recent "any time allowed" Friday
FRIDAY_CADENCE_DAYS = 14          # every other Friday
MIN_NOTICE_MINUTES = 30           # don't alert for slots starting sooner than this

STATE_PATH = Path(__file__).resolve().parent / "state.json"
LTA_API = "https://www.lta.org.uk/api/courtdetail/availability?venueid={venue_id}&date={day}"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ------------------------------ helpers ------------------------------


def http_get_json(url: str):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def parse_start(slot_iso: str) -> datetime:
    """Parse an ISO start time; treat naive values as London local time."""
    if slot_iso.endswith("Z"):
        slot_iso = slot_iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(slot_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LONDON)
    return dt.astimezone(LONDON)


def is_on_friday(d: date) -> bool:
    """True on 'on' Fridays: 2026-10-09, 2026-10-23, 2026-11-06, ..."""
    return d.weekday() == 4 and (d - ON_FRIDAY_ANCHOR).days % FRIDAY_CADENCE_DAYS == 0


def slot_matches(start: datetime) -> bool:
    d = start.date()
    if d.weekday() >= 5:          # Saturday / Sunday: any time
        return True
    if is_on_friday(d):          # every other Friday: any time
        return True
    t = start.time()             # other weekdays: early or evening only
    return t < EARLY_CUTOFF or t >= EVENING_FROM


def fetch_slots(venue: dict, days: list) -> dict:
    """Return {day: set('CourtName|startTimeISO')} for one venue."""
    result = {}
    for day in days:
        url = LTA_API.format(venue_id=venue["id"], day=day)
        try:
            data = http_get_json(url)
        except Exception as exc:
            print(f"[warn] {venue['name']} {day}: {exc}", file=sys.stderr)
            continue
        slots = set()
        for court in data.get("venueDetails") or []:
            if court.get("name", "").lower().startswith("mini"):
                continue          # skip kids' mini courts
            for slot in court.get("availableSlots") or []:
                start_iso = slot.get("startTime")
                if start_iso:
                    slots.add(f"{court['name']}|{start_iso}")
        result[day] = slots
    return result


def send_telegram(message: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[warn] Telegram not configured; printing instead:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"[error] Telegram HTTP {exc.code}: {body}", file=sys.stderr)
        print(f"[debug] chat_id as stored: {TELEGRAM_CHAT_ID!r} "
              f"(token set: {bool(TELEGRAM_BOT_TOKEN)}, len {len(TELEGRAM_BOT_TOKEN)})", file=sys.stderr)
        raise


# ------------------------------ main ------------------------------


def main() -> int:
    now = datetime.now(LONDON)
    today = now.date()
    days = [(today + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD + 1)]

    prev_state = None
    if STATE_PATH.exists():
        try:
            prev_state = json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            prev_state = None
    first_run = prev_state is None   # prime silently so we only alert on changes
    prev_state = prev_state or {}

    new_state = {}
    alerts = []

    for venue in VENUES:
        day_slots = fetch_slots(venue, days)
        for day, slots in day_slots.items():
            key = f"{venue['id']}|{day}"
            previous = set(prev_state.get(key, []))
            new_state[key] = sorted(slots)
            if first_run:
                continue
            for entry in slots - previous:
                court, start_iso = entry.split("|", 1)
                start = parse_start(start_iso)
                if start <= now + timedelta(minutes=MIN_NOTICE_MINUTES):
                    continue
                if slot_matches(start):
                    alerts.append((start, venue, court, day))

    STATE_PATH.write_text(json.dumps(new_state, indent=2, sort_keys=True))

    if alerts:
        alerts.sort(key=lambda a: a[0])
        lines = ["🎾 New tennis slots!"]
        for start, venue, court, day in alerts:
            lines.append(
                f"• {venue['name']} — {court}: "
                f"{start.strftime('%a %d %b')}, {start.strftime('%H:%M')}"
            )
            lines.append(f"  Book: {venue['book_url'].format(date=day)}")
        message = "\n".join(lines)
        send_telegram(message)
        print(message)
    else:
        print("No new matching slots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
