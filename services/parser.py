import json
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from services.clock import app_timezone


MONTHS = {
    "jan": 1, "fev": 2, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "mai": 5, "may": 5,
    "jun": 6, "jul": 7, "ago": 8, "aug": 8, "set": 9, "sep": 9, "out": 10, "oct": 10,
    "nov": 11, "dez": 12, "dec": 12,
}
DATE_PATTERN = re.compile(r"([a-zç]{3})\.?\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(.*)")

# Filename fragments checked in order; "followers" must come before "following".
KIND_BY_FILENAME = (
    ("followers", "followers"),
    ("following", "following"),
    ("pending", "pending_sent"),
    ("received", "pending_received"),
    ("blocked", "blocked"),
    ("close_friends", "close_friends"),
    ("favorited", "favorites"),
    ("unfollowed", "recently_unfollowed"),
    ("removed_suggestions", "removed_suggestions"),
    ("custom_lists", "custom_lists"),
)


def detect_kind(file_path, filename):
    filename = filename.lower()
    for fragment, kind in KIND_BY_FILENAME:
        if fragment in filename:
            return kind

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
            title = (soup.title.string or "").lower() if soup.title else ""
            if "seguidores" in title or "followers" in title:
                return "followers"
            if "seguindo" in title or "following" in title:
                return "following"
    except Exception:
        pass
    return None


def _username_from_href(href):
    if not href or "instagram.com" not in href:
        return None
    path = urlparse(href).path.strip("/")
    if path.startswith("_u/"):
        path = path[3:]
    username = path.split("/")[0].strip()
    return username or None


def parse_instagram_date(date_str):
    """Parse the dates of the HTML export, in Portuguese or English.

    Examples: 'jan 06, 2019 6:21 da tarde', 'dez 03, 2025 3:56 da manhã',
    'Jan 06, 2019 6:21 pm'. Returns None when the text is not a recognised
    date so callers never invent one.
    """
    if not date_str:
        return None
    match = DATE_PATTERN.search(date_str.strip().lower())
    if not match:
        return None
    month_text, day, year, hour, minute, period = match.groups()
    month = MONTHS.get(month_text)
    if month is None:
        return None
    hour = int(hour)
    if any(token in period for token in ("tarde", "noite", "pm")):
        if hour != 12:
            hour += 12
    elif any(token in period for token in ("manhã", "manha", "madrugada", "am")):
        if hour == 12:
            hour = 0
    try:
        return datetime(int(year), month, int(day), hour, int(minute))
    except ValueError:
        return None


def parse_instagram_html(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    entries = soup.find_all("div", class_="_a6-p") or soup.find_all("tr")

    data_list = []
    for entry in entries:
        link = entry.find("a", href=True)
        if not link:
            continue
        username = _username_from_href(link["href"])
        if not username:
            continue

        date_obj = None
        if entry.name == "div":
            date_div = link.parent.find_next_sibling("div")
            if date_div:
                date_obj = parse_instagram_date(date_div.text)
        elif entry.name == "tr":
            cells = entry.find_all("td")
            if len(cells) > 1:
                date_obj = parse_instagram_date(cells[-1].text)

        data_list.append({"username": username, "date": date_obj})

    return data_list


def parse_instagram_json(file_path):
    """Read both current and legacy Instagram relationship JSON structures."""
    with open(file_path, "r", encoding="utf-8") as file_handle:
        payload = json.load(file_handle)

    parsed = []

    def walk(value):
        if isinstance(value, dict):
            string_data = value.get("string_list_data")
            if isinstance(string_data, list):
                for item in string_data:
                    if not isinstance(item, dict):
                        continue
                    username = (item.get("value") or _username_from_href(item.get("href")) or "").strip()
                    timestamp = item.get("timestamp")
                    try:
                        date_value = datetime.fromtimestamp(int(timestamp), app_timezone()).replace(tzinfo=None) if timestamp else None
                    except (TypeError, ValueError, OSError):
                        date_value = None
                    if username:
                        parsed.append({"username": username, "date": date_value})
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    return parsed


def parse_instagram_file(file_path):
    if str(file_path).lower().endswith(".json"):
        return parse_instagram_json(file_path)
    return parse_instagram_html(file_path)
