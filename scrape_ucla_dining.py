"""
UCLA Dining menu scraper
=========================
Pulls today's menu from four separate per-hall pages on dining.ucla.edu
(Bruin Plate, De Neve Dining, Epicuria at Covel, Feast at Rieber) and turns
it into structured JSON: one row per menu item, tagged with which dining
hall, which meal period, which station, and which allergen/diet icons it
carries (Fish, Vegan, Halal, etc).

Run it:
    python scrape_ucla_dining.py > menu_today.json

Then feed menu_today.json into the search app (see the published
artifact) or into your own alert-checking script.

NOTES / TODO before you rely on this daily:
 - Each hall page has a "Jump to date" picker (Yesterday / Today / Tomorrow
   / named weekdays) — worth checking devtools on one of these pages to see
   if those are plain links with a ?date= or similar param, which would let
   this script pull a specific day instead of only "today."
 - The parser deliberately doesn't hard-code exact heading depths (h2 vs h3
   vs h4) since that couldn't be confirmed against the live raw HTML while
   writing this — see parse_hall_page()'s docstring for how it identifies
   items instead. If items start turning up missing or misattributed to
   the wrong station, that's the first place to check.
 - Be a polite scraper: cache results, don't hit these pages more than
   once every ~15 minutes, and set a real User-Agent.
"""

import json
import re
import sys
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

# Switched from the combined "menus-at-a-glance" page to these per-hall
# pages: each one carries breakfast+lunch+dinner together and prints its
# own "Today, <date>" line, so a single hall's cache being stale doesn't
# take the whole app down with it the way the combined page did.
HALL_PAGES = {
    "Bruin Plate": "https://dining.ucla.edu/bruin-plate/",
    "De Neve Dining": "https://dining.ucla.edu/de-neve-dining/",
    "Epicuria at Covel": "https://dining.ucla.edu/epicuria-at-covel/",
    "Feast at Rieber": "https://dining.ucla.edu/spice-kitchen/",
}

# Matches the page's own "Today, September 22, 2026" line.
PAGE_DATE_RE = re.compile(r"Today,\s*([A-Za-z]+ \d{1,2},?\s*\d{4})")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UCLADiningSearchBot/0.1; "
                  "personal student project, not affiliated with UCLA Dining)"
}

# Allergen/diet icon filenames -> clean tag names.
# The site renders each as <img ... alt="Fish"> etc, so we just read `alt`.
# Kept separate on purpose: fish and shellfish are different allergies/diets
# (someone who's fine with salmon may still avoid shrimp, or vice versa).
FISH_TAG = "Fish"
SHELLFISH_TAG = "Crustacean-shellfish"

# Rough keyword list for "this is a protein item" when there's no structured
# tag for it (UCLA's allergen icons cover Fish/Shellfish/Vegan/Halal, but not
# "this dish is chicken" or "this dish is beef"). Matched against the item
# name, case-insensitive. Extend this list as you find items it misses.
PROTEIN_KEYWORDS = [
    "chicken", "beef", "steak", "turkey", "pork", "bacon", "sausage", "ham",
    "salmon", "shrimp", "tuna", "cod", "lamb", "meatball", "burger",
    "beyond", "impossible", "tofu", "tempeh", "egg", "ribs",
]


def fetch_html(url: str) -> str:
    # Cache-busting: some CDNs/caching plugins key their cache on the exact
    # URL, so a throwaway query param plus no-cache headers gives the best
    # chance of getting a truly fresh copy instead of a stale cached one.
    params = {"_": str(int(datetime.now().timestamp()))}
    headers = {**HEADERS, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_hall_page(html: str, hall_name: str):
    """Returns (items, menu_date) for one dining hall's page. menu_date
    comes from the page's own "Today, <date>" line, or None if that
    couldn't be found/parsed.

    This walks every heading tag (h1-h5) plus every <img> and <a> tag in
    document order, rather than assuming a fixed heading depth for
    meal/station/item, since that depth couldn't be confirmed against the
    live raw HTML while writing this. An item is identified by the
    "/menu-item/?recipe=" link UCLA puts under every dish — that pattern is
    the most reliable anchor regardless of heading nesting. If UCLA changes
    their page structure and this stops finding items, that recipe-link
    check is the first thing to verify.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    menu_date = None

    date_m = PAGE_DATE_RE.search(soup.get_text())
    if date_m:
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                menu_date = datetime.strptime(date_m.group(1).strip(), fmt).date().isoformat()
                break
            except ValueError:
                continue

    MEAL_WORDS = {"BREAKFAST", "LUNCH", "DINNER"}
    current_meal = None
    current_station = None
    last_heading_text = None
    pending_tags = []

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "a", "img"]):
        if tag.name in ("h1", "h2", "h3", "h4", "h5"):
            text = tag.get_text(strip=True)
            if not text:
                continue
            upper = text.upper()
            if upper in MEAL_WORDS:
                current_meal = upper.title()
                current_station = None
                last_heading_text = None
                pending_tags = []
                continue
            if last_heading_text is not None:
                # The previous heading was never followed by a recipe link,
                # so it must have been a station name, not an item name.
                current_station = last_heading_text
            last_heading_text = text
            pending_tags = []
        elif tag.name == "img":
            alt = tag.get("alt", "").strip()
            if alt:
                pending_tags.append(alt)
        elif tag.name == "a":
            href = tag.get("href", "")
            if "/menu-item/?recipe=" in href and last_heading_text:
                items.append({
                    "hall": hall_name,
                    "meal": current_meal,
                    "station": current_station,
                    "name": last_heading_text,
                    "tags": pending_tags,
                    "has_fish": FISH_TAG in pending_tags,
                    "has_shellfish": SHELLFISH_TAG in pending_tags,
                    "recipe_url": href,
                })
                last_heading_text = None
                pending_tags = []

    return items, menu_date


def is_protein_item(item: dict) -> bool:
    """True if this looks like a protein-centric dish rather than a side,
    bread, dessert, drink, or plain vegetable. Fish/Shellfish are caught by
    their structured tags; everything else falls back to a name keyword
    match, which is approximate — see PROTEIN_KEYWORDS above."""
    if item["has_fish"] or item["has_shellfish"]:
        return True
    name_lower = item["name"].lower()
    return any(kw in name_lower for kw in PROTEIN_KEYWORDS)


def main():
    all_items = []
    hall_dates = {}

    for hall_name, url in HALL_PAGES.items():
        html = fetch_html(url)
        items, menu_date = parse_hall_page(html, hall_name)
        all_items.extend(items)
        hall_dates[hall_name] = menu_date

    protein_items = [it for it in all_items if is_protein_item(it)]
    run_date = date.today().isoformat()

    # Use the most common date seen across hall pages as the overall
    # "scraped_date" — if one hall's page happens to be stale while the
    # others aren't, this doesn't let that one hall drag the whole app's
    # displayed date backward.
    found_dates = [d for d in hall_dates.values() if d]
    menu_date = max(set(found_dates), key=found_dates.count) if found_dates else None

    is_stale = False
    if menu_date:
        days_behind = (date.today() - date.fromisoformat(menu_date)).days
        is_stale = days_behind >= 1

    output = {
        "scraped_date": menu_date or run_date,
        "date_extracted_from_page": menu_date is not None,
        "scraped_at": run_date,
        "is_stale": is_stale,
        # Per-hall dates, so you can see at a glance if just one hall's
        # page was stale rather than all of them.
        "hall_dates": hall_dates,
        "item_count": len(protein_items),
        "total_items_seen": len(all_items),
        "items": protein_items,
    }
    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
