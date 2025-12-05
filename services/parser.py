from bs4 import BeautifulSoup
from datetime import datetime
import re

def detect_kind(file_path, filename):
    filename = filename.lower()

    if "followers" in filename: return "followers"
    if "following" in filename: return "following"
    if "pending" in filename: return "pending_sent"
    if "received" in filename: return "pending_received"
    if "blocked" in filename: return "blocked"
    if "close_friends" in filename: return "close_friends"
    if "favorited" in filename: return "favorites"
    if "unfollowed" in filename: return "recently_unfollowed"
    if "removed_suggestions" in filename: return "removed_suggestions"
    if "custom_lists" in filename: return "custom_lists"
    

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
            title = soup.title.string.lower() if soup.title else ""
            if "seguidores" in title: return "followers"
            if "seguindo" in title: return "following"
    except: pass
    return None

def parse_date_portuguese(date_str):
    """
    Lê datas como: 
    'jan 06, 2019 6:21 da tarde/noite' 
    'dez 03, 2025 3:56 da manhã'
    """
    if not date_str: return datetime.now()

    months = {
        'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
        'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
    }

    try:
        date_str = date_str.strip().lower()

        match = re.search(r"([a-z]{3})\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s+(.*)", date_str)
        
        if match:
            mon_str, day, year, hour, minute, period = match.groups()
            month = months.get(mon_str, 1)
            hour = int(hour)
         
            if "tarde" in period or "noite" in period:
                if hour != 12: hour += 12
            elif "manhã" in period:
                if hour == 12: hour = 0
                
            return datetime(int(year), month, int(day), hour, int(minute))
            
    except Exception:
        pass
    
    return datetime.now()

def parse_instagram_html(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    data_list = []

    entries = soup.find_all("div", class_="_a6-p")
    
    if not entries:
        entries = soup.find_all("tr")

    for entry in entries:
        try:
            link = entry.find("a", href=True)
            if not link: continue
            
            href = link["href"]
            if "instagram.com" in href:
                username = href.replace("https://www.instagram.com/", "").replace("_u/", "").strip("/").strip()
                
                date_obj = datetime.now()
      
                if entry.name == 'div':
                    date_div = link.parent.find_next_sibling("div")
                    if date_div: date_obj = parse_date_portuguese(date_div.text)
             
                elif entry.name == 'tr':
                 
                    tds = entry.find_all("td")
                    if len(tds) > 1:
                        date_obj = parse_date_portuguese(tds[-1].text)

                if username:
                    data_list.append({
                        "username": username,
                        "date": date_obj
                    })
        except:
            continue

    return data_list