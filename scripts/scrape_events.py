import requests
from bs4 import BeautifulSoup
from supabase import create_client
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import os
import html
from pathlib import Path
import re

# ------------------------------------------
# --------------  METHODS  -----------------
# ------------------------------------------

# ---------------- HELPERS -------------
# helper function to collapse internal whitespace and strip
def norm_text(s):
    if s:
        return " ".join(s.split()).strip()
    else:
        return ""

# --------- DB METHODS ------------
# returns list of all unique event_ids from event_guid_list table from LeukerbaDB
def fetch_all_event_ids(supabase):
    resp = (
        supabase
        .table("event_guid_list")
        .select("event_id")
        .execute()
    )

    if not resp.data:
        return []

    # Extract and return as a simple list of strings
    return [row["event_id"] for row in resp.data if row.get("event_id") is not None]

# upsert final list of dicts to database
def upsert_records(supabase_client, table: str, records: list[dict], on_conflict: str, chunk_size: int = 1000):
    if not records:
        return

    # chunked upserts
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i+chunk_size]
        supabase_client.table(table).upsert(
            chunk,
            on_conflict=on_conflict
        ).execute()

# -------- SCRAPE METHODS ----------
def extract_event_header(soup: BeautifulSoup) -> dict:
    title_el = soup.select_one('h1[data-selector="event-title"]') or soup.select_one('h1.heading-2')
    summary_el = soup.select_one('.event-intro-text')

    title = norm_text(title_el.get_text(" ")) if title_el else None

    # summary is inside .event-intro-text, often wrapped in <p class="dw">...</p>
    summary = norm_text(summary_el.get_text(" ")) if summary_el else None

    return {"title": title, "summary": summary}

def extract_event_overview(soup: BeautifulSoup) -> dict:
    tz = ZoneInfo("Europe/Zurich")

    # Scope to the overview area (if page has multiple lists)
    root = soup.select_one("ul.event-intro-list")
    if not root:
        return {
            "top_event": False,
            "location": None,
            "next_date": None,
            "duration": None,
            "price": None,
            "booking_url": None,
        }

    top_event = soup.select_one(".top-event-badge") is not None

    def value_for(label: str) -> str | None:
        """Find <li> where the first <span> matches label, return text from the <b> (preferred)"""
        for li in root.select("li"):
            k = li.select_one(":scope > span")
            if not k:
                continue
            if norm_text(k.get_text(" ")) == label:
                b = li.select_one("b")
                return norm_text((b or li).get_text(" "))
        return None

    location = value_for("Ort")
    date_str = value_for("Datum")       # e.g. 07.12.2025
    time_str = value_for("Startzeit")   # e.g. 08:30
    duration = value_for("Dauer")       # e.g. 02:00 h

    # Price lives outside the <ul>
    price_box = root.find_next(lambda t: getattr(t, "name", None) == "div" and "items-baseline" in (t.get("class") or []))
    price = None
    if price_box:
        # this normalizes the whitespace "CHF         51.–" -> "CHF 51.–"
        price = norm_text(price_box.get_text(" "))

    # booking url: first CTA after the list
    booking_a = root.find_next("a", href=True)
    booking_url_ending = booking_a.get("href")

    if booking_url_ending.startswith("/shop"): # all internal ref links start with shop -> attach leukerbad.ch
        booking_url = "https://leukerbad.ch" + booking_url_ending

    elif booking_url_ending.startswith("https://"): # catch all other urls - put them in as is
        booking_url = booking_url_ending

    else: # everything else is not a booking link, so there is no booking (finds next href which is something else)
        booking_url = None

    # Combine date+time -> ISO timestamptz
    next_date = None
    if date_str:
        # Default time if missing (some events might not have Startzeit)
        t = time_str or "00:00"
        # Parse Swiss date format DD.MM.YYYY
        dt = datetime.strptime(f"{date_str} {t}", "%d.%m.%Y %H:%M").replace(tzinfo=tz)
        next_date = dt.isoformat()  # e.g. "2025-12-07T08:30:00+01:00"

    return {
        "top_event": top_event,
        "location": location,
        "next_date": next_date,     # ISO string suitable for timestamptz insert/upsert
        "duration": duration,
        "price": price,             # e.g. "ab CHF 51.–" depending on surrounding text
        "booking_url": booking_url,
    }

def extract_event_all_dates(soup: BeautifulSoup) -> list[str]:
    tz = ZoneInfo("Europe/Zurich")

    section = soup.find(lambda t: getattr(t, "name", None) == "section"
                        and t.find(string=re.compile(r"Weitere Termine", re.I)))
    if not section:
        return []

    dates = []
    cards = section.select(".grid > div.flex.flex-col")  # each date card

    for card in cards:
        # First <b> usually is the date, second <b> usually is time
        bs = card.select("span > b")
        if not bs:
            continue

        date_raw = norm_text(bs[0].get_text(" "))  # "Sonntag, 14.12.2025"
        time_raw = norm_text(bs[1].get_text(" ")) if len(bs) > 1 else "00:00"

        # Extract dd.mm.yyyy from "weekday, dd.mm.yyyy"
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_raw)
        if not m:
            continue
        date_str = m.group(1)

        # Extract HH:MM from time (ignore duration like "02:00 h")
        tm = re.search(r"(\d{2}:\d{2})", time_raw)
        time_str = tm.group(1) if tm else "00:00"

        dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M").replace(tzinfo=tz)
        dates.append(dt.isoformat())  # "2025-12-14T08:30:00+01:00"

    # de-dupe while preserving order
    seen = set()
    out = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out

def extract_event_contact_block(soup: BeautifulSoup) -> str | None:
    # Find the "Kontakt" heading first, then take the nearby container
    h = soup.find(lambda t: getattr(t, "name", None) in {"h2", "h3", "h4"}
                  and norm_text(t.get_text(" ")).lower() == "kontakt")
    if not h:
        return None

    # Usually the content sits in the next div(s); grab a reasonable parent/container
    container = h.find_parent("div") or h.parent
    if not container:
        return None

    # Prefer the next sibling block if it looks more like the contact chunk
    cand = container
    sib = container.find_next_sibling("div")
    if sib and (sib.select_one('a[href^="tel:"]') or "Tel." in sib.get_text()):
        cand = sib

    # get_text collapses <br> nicely with a separator
    text = norm_text(cand.get_text(" ", strip=True))
    if not text:
        return None

    # Optional: remove the enkoder placeholder noise
    text = re.sub(r"email hidden; JavaScript is required", "", text, flags=re.I).strip()
    text = re.sub(r"\s{2,}", " ", text).strip()

    return text or None

# extractor for tab section of the events page. Returns price info separately and otherwise everything in one string (description)
def extract_event_prices_and_description(soup: BeautifulSoup) -> dict:
    prices_parts: list[str] = []
    desc_parts: list[str] = []

    containers = soup.select('div[data-selector="tab-content-container"]')

    for container in containers:
        # each sibling block tends to be a direct child <div> with an H2 inside
        blocks = container.select(':scope > div') or container.select('div')

        for block in blocks:
            h2 = block.select_one('h2.heading-5.pb-2')
            if not h2:
                continue

            heading = norm_text(h2.get_text(" "))
            # Remove the heading itself from the captured content
            body_text = norm_text(block.get_text("\n", strip=True))
            if not body_text:
                continue

            # Drop heading from top of body_text if present
            if body_text.startswith(heading):
                body_text = norm_text(body_text[len(heading):].strip())

            # Clean common noise
            body_text = re.sub(r"email hidden; JavaScript is required", "", body_text, flags=re.I).strip()
            body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()

            if not body_text:
                continue

            if heading.lower() == "preis information":
                prices_parts.append(body_text)
            else:
                desc_parts.append(f"{heading}\n{body_text}" if heading else body_text)

    all_prices = "\n\n".join(prices_parts).strip() or None
    description = "\n\n".join(desc_parts).strip() or None

    return {"all_prices": all_prices, "description": description}

# ------------------------------------------
# ----------------  MAIN  ------------------
# ------------------------------------------

# get timestamp info - for updated_at
updated_at = datetime.now(timezone.utc).isoformat()

# Resolve the project root (two levels up if needed)
project_root = Path(__file__).resolve().parent.parent
env_path = project_root / ".env"

# get env to connect to supabase
if env_path.exists():
    load_dotenv()

# read config
url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]

# connect to supabase LeukerbaDB project
supabase = create_client(url, key)

event_ids = fetch_all_event_ids(supabase)
print(event_ids)

# empty list of records - will become list of dicts to upsert to db
records = []

# iterate through list of event_ids
for id in event_ids:
    # logging - print id
    print(id)

    # make url from base url + tour id
    url = 'https://leukerbad.ch/event/' + str(id)

    # call url and scrape to soup
    try:
        resp = requests.get(url, timeout=20)

        if resp.status_code == 404:
            # skip missing events (which give a 404)
            continue

        resp.raise_for_status()  # still fail on other 4xx/5xx
        soup = BeautifulSoup(resp.text, "html.parser")

        # call extraction methods on soup
        header = extract_event_header(soup)
        overview = extract_event_overview(soup)
        all_dates = extract_event_all_dates(soup) # list of strings in timestamptz format
        contact = extract_event_contact_block(soup) # string with contact info
        tabs = extract_event_prices_and_description(soup) # dict of two strings, price and everything else in description

        # collect all info in list of dicts
        records.append({
            "event_id": id,
            "language": "de",
            "ref_url": url,
            "title": header.get("title"),
            "summary": header.get("summary"),
            "top_event": overview.get("top_event"),
            "location": overview.get("location"),
            "next_date": overview.get("next_date"),
            "duration": overview.get("duration"),
            "price": overview.get("price"),
            "all_dates": all_dates,
            "all_prices": tabs.get("all_prices"),
            "description": tabs.get("description"),
            "booking_url": overview.get("booking_url"),
            "contact": contact,
            "updated_at": updated_at
        })

    except requests.exceptions.RequestException as e:
        # network error, timeout, 500s, etc. - skip (or log)
        continue

# upsert to Supabase table events
upsert_records(
    supabase,
    table="events",
    records=records,
    on_conflict="event_id,language",
    chunk_size=1000,
)

print("Successfully updated events table.")