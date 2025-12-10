import requests
from bs4 import BeautifulSoup
from supabase import create_client
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
import os
import html
from pathlib import Path

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
# returns list of all unique tour_ids from oa_infosnow_mapping table from LeukerbaDB
def fetch_all_tour_ids(supabase):
    resp = (
        supabase
        .table("oa_infosnow_mapping")
        .select("tour_id")
        .execute()
    )

    if not resp.data:
        return []

    # Extract and return as a simple list of ints
    return [row["tour_id"] for row in resp.data if row.get("tour_id") is not None]

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
# extracts header info - title, summary, short description
def extract_hike_header(soup: BeautifulSoup):
    result = {
        "title": None,
        "summary": None,
        "description": None,
    }

    # title
    title_el = soup.select_one('h1[data-selector="tour-title"]')
    if title_el:
        result["title"] = norm_text(title_el.get_text(" "))

    # summary
    summary_el = soup.select_one('.tour-intro-short-text')
    if summary_el:
        result["summary"] = norm_text(summary_el.get_text(" "))

    # description
    desc_block = None

    if summary_el:
        # walk forward through siblings until it finds a good text div
        sib = summary_el
        while True:
            sib = sib.find_next_sibling()
            if sib is None:
                break
            if sib.name != "div":
                continue

            classes = sib.get("class") or []

            # skip obvious non-description containers
            if "tour-intro-short-text" in classes:
                continue
            if "tour-intro-author" in classes:
                continue
            if "image-slider-wrapper" in classes:
                continue
            if "slider" in classes:
                continue
            if "md:hidden" in classes and "mb-10" in classes:
                # your mobile slider wrapper
                continue

            text = norm_text(sib.get_text(" "))
            # require some length so we don't accidentally grab tiny scraps
            if text and len(text) > 80:
                desc_block = sib
                break

    if desc_block:
        result["description"] = norm_text(desc_block.get_text(" "))

    return result

# extracts info from right-hand module (difficulty, distance, time, uphill, downhill)
def extract_hike_overview(soup: BeautifulSoup) -> dict:
    result = {
        "difficulty": None,
        "distance": None,
        "time": None,
        "uphill": None,
        "downhill": None,
    }

    ul = soup.select_one('ul.tour-intro-list')
    if not ul:
        return result

    for li in ul.select('li'):
        label_span = li.find('span')
        if not label_span:
            continue

        label = norm_text(label_span.get_text(" "))

        # difficulty (special: uses a widget div)
        if label == "Schwierigkeitsgrad":
            widget = li.select_one('#app-OutdoorActiveTourDifficultyTagsWidget')
            if widget:
                raw_code = widget.get('data-tour-difficulty')  # e.g. "&quot;1&quot;"
                if raw_code:
                    # normalise: strip quotes and HTML-escaped quotes
                    code = raw_code.replace('&quot;', '').strip(' "\'')
                    # map OA codes (1/2/3) to German labels
                    difficulty_map = {
                        "1": "leicht",
                        "2": "mittel",
                        "3": "schwer",
                    }
                    result["difficulty"] = difficulty_map.get(code, code)

        # Simple label + <b> value pairs
        elif label == "Distanz":
            b = li.select_one('span b')
            if b:
                result["distance"] = norm_text(b.get_text(" "))

        elif label == "Zeit":
            b = li.select_one('span b')
            if b:
                result["time"] = norm_text(b.get_text(" "))

        elif label == "Aufstieg":
            b = li.select_one('span b')
            if b:
                result["uphill"] = norm_text(b.get_text(" "))

        elif label == "Abstieg":
            b = li.select_one('span b')
            if b:
                result["downhill"] = norm_text(b.get_text(" "))

    return result

# extract long text & season info
def extract_hike_details(soup: BeautifulSoup):
    result = {
        "trail_directions": None,
        "season_months": None,
    }

    widget = soup.select_one('#app-OutdoorActiveTabsWidget')
    if not widget:
        return result

    raw = widget.get('data-tour')
    if not raw:
        return result

    try:
        # fix HTML entities (&quot;, &amp; etc)
        decoded = html.unescape(raw)
        # parse JSON into a Python dict
        tour = json.loads(decoded)
    except Exception as e:
        print("Failed to parse data-tour JSON:", e)

        return result

    # more detailed description into trail_directions from "directions" field
    directions_html = tour.get("directions")
    if directions_html:
        directions_html = html.unescape(directions_html)
        directions_soup = BeautifulSoup(directions_html, "html.parser")
        # tidy through norm_text
        result["trail_directions"] = norm_text(directions_soup.get_text(" "))

    # -season: map jan..dec booleans to [1..12] to make list of months
    season_obj = tour.get("season") or {}
    month_keys = ["jan", "feb", "mar", "apr", "may",
                  "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    season_months = []

    for idx, key in enumerate(month_keys, start=1):
        if season_obj.get(key):
            season_months.append(idx)

    if season_months:
        result["season_months"] = season_months

    return result

# extract tags from <span class="tour-intro-tag">…</span>. to list of strings
def extract_hike_tags(soup: BeautifulSoup):
    tags = []
    for span in soup.select('.tour-intro-tag'):
        txt = norm_text(span.get_text(" "))
        if txt:
            tags.append(txt)
    return tags


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

# collect tour_ids to iterate from database (stored in oa_infosnow_mapping)
tour_ids = fetch_all_tour_ids(supabase)

# empty list of records - will become list of dicts to upsert to db
records = []

# iterate through list of tour_ids
for id in tour_ids:
    # logging - print id
    print(id)

    # make url from base url + tour id
    url = 'https://leukerbad.ch/tour/' + str(id)

    # call url and scrape to soup
    try:
        resp = requests.get(url, timeout=20)

        if resp.status_code == 404:
            # skip missing events (which give a 404)
            continue

        resp.raise_for_status()  # still fail on other 4xx/5xx
        soup = BeautifulSoup(resp.text, "html.parser")

        # call extraction methods on soup
        basics = extract_hike_header(soup)
        overview = extract_hike_overview(soup)
        details = extract_hike_details(soup)
        tags = extract_hike_tags(soup)

        # collect all info in list of dicts
        records.append({
            "tour_id": id,
            "language": "de",
            "ref_url": url,
            "title": basics.get("title"),
            "summary": basics.get("summary"),
            "description": basics.get("description"),
            "difficulty": overview.get("difficulty"),
            "distance": overview.get("distance"),
            "time": overview.get("time"),
            "uphill": overview.get("uphill"),
            "downhill": overview.get("downhill"),
            "trail_directions": details.get("trail_directions"),
            "season": details.get("season_months"),  # list of month numbers
            "tags": tags,  # list of strings
            "updated_at": updated_at,
        })

    except requests.exceptions.RequestException as e:
        # network error, timeout, 500s, etc. - skip (or log)
        continue

# upsert to Supabase oa_tours
upsert_records(
    supabase,
    table="oa_tours",
    records=records,
    on_conflict="tour_id,language",
    chunk_size=1000,
)

print("Successfully updated oa_tours table.")