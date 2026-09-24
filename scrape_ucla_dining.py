"""
UCLA Dining menu scraper
=========================
Pulls today's menu from dining.ucla.edu/menus-at-a-glance/ (UCLA's live
JAMIX-powered menu page) and turns it into structured JSON: one row per
menu item, tagged with which dining hall, which meal period, which
station, and which allergen/diet icons it carries (Fish, Vegan, Halal,
etc).

Run it:
    python scrape_ucla_dining.py > menu_today.json

Then feed menu_today.json into the search app (see the published
artifact) or into your own alert-checking script.

NOTES / TODO before you rely on this daily:
 - This scrapes the "today" view. The live page has a "Change Date"
   date-picker that appears to be JS-driven rather than a simple
   ?date= URL param — open the page in a browser, open devtools ->
   Network, click a future date, and see what request fires. It's
   likely either a query param on the same URL or a call to a
   JAMIX-hosted JSON endpoint (jamix.cloud) — if it's the latter,
   hitting that endpoint directly would be even more reliable than
   scraping HTML at all. Update FUTURE_DATE_URL_TEMPLATE below once
   you find it.
 - Page structure (WordPress + JAMIX plugin) can change; the CSS
   selectors below match the structure as of September 2026.
 - Be a polite scraper: cache results, don't hit the page more than
   once every ~15 minutes, and set a real User-Agent.
"""

import json
import re
import sys
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

MENU_URL = "https://dining.ucla.edu/menus-at-a-glance/"

# Matches the date UCLA prints in their own heading, e.g.
# "BREAKFAST MENU FOR TODAY, SEPTEMBER 17, 2026" -> "SEPTEMBER 17, 2026"
PAGE_DATE_RE = re.compile(r"([A-Za-z]+ \d{1,2},?\s*\d{4})")

# If you find the real date-param pattern, put it here, e.g.:
# FUTURE_DATE_URL_TEMPLATE = "https://dining.ucla.edu/menus-at-a-glance/?date={date}"
FUTURE_DATE_URL_TEMPLATE = None

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


def fetch_menu_html(target_date: date | None = None) -> str:
    url = MENU_URL
    if target_date and FUTURE_DATE_URL_TEMPLATE:
        url = FUTURE_DATE_URL_TEMPLATE.format(date=target_date.isoformat())
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_menu(html: str):
    """Returns (items, menu_date). menu_date is the date UCLA's own page
    says the menu is for (parsed straight out of their heading text), or
    None if that text couldn't be found/parsed — in which case the caller
    should treat the data's freshness as unverified rather than assuming
    it's "today"."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    menu_date = None

    # The page is organized: H2 "BREAKFAST MENU FOR TODAY..." / H3 hall name
    # / H4 station name / <ul><li><a>Item Name</a><img alt="Tag">...</li></ul>
    meal_heading = None
    hall_heading = None
    station_heading = None

    for el in soup.find_all(["h2", "h3", "h4", "li"]):
        if el.name == "h2":
            text = el.get_text(strip=True)
            m = re.search(r"(BREAKFAST|LUNCH|DINNER)", text, re.I)
            if m:
                meal_heading = m.group(1).title()
            if menu_date is None:
                date_m = PAGE_DATE_RE.search(text)
                if date_m:
                    for fmt in ("%B %d, %Y", "%B %d %Y"):
                        try:
                            menu_date = datetime.strptime(
                                date_m.group(1).replace(",", ", ").replace(",  ", ", "),
                                fmt,
                            ).date().isoformat()
                            break
                        except ValueError:
                            continue
        elif el.name == "h3":
            hall_heading = el.get_text(strip=True)
        elif el.name == "h4":
            station_heading = el.get_text(strip=True)
        elif el.name == "li":
            link = el.find("a")
            if not link or not hall_heading:
                continue
            name = link.get_text(strip=True)
            if not name:
                continue
            tags = [
                img.get("alt", "").strip()
                for img in el.find_all("img")
                if img.get("alt", "").strip()
            ]
            items.append({
                "hall": hall_heading,
                "meal": meal_heading,
                "station": station_heading,
                "name": name,
                "tags": tags,
                "has_fish": FISH_TAG in tags,
                "has_shellfish": SHELLFISH_TAG in tags,
                "recipe_url": link.get("href", ""),
            })

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
    html = fetch_menu_html()
    items, menu_date = parse_menu(html)
    protein_items = [it for it in items if is_protein_item(it)]
    run_date = date.today().isoformat()
    output = {
        # The date UCLA's own page says the menu is for. Falls back to the
        # run date only if that text couldn't be parsed — check
        # "date_extracted_from_page" before trusting this on a day UCLA
        # changes their page layout.
        "scraped_date": menu_date or run_date,
        "date_extracted_from_page": menu_date is not None,
        "scraped_at": run_date,
        "item_count": len(protein_items),
        "total_items_seen": len(items),
        "items": protein_items,
    }
    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
