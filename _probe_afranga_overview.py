import re
from dotenv import load_dotenv
load_dotenv()

import requests
from diversification.afranga_diversification import login, PROFILE_OVERVIEW_URL, _HEADERS

session = requests.Session()
login(session)

r = session.get(PROFILE_OVERVIEW_URL, headers=_HEADERS, timeout=20)
r.raise_for_status()
html = r.text

with open("_afranga_overview.html", "w", encoding="utf-8") as f:
    f.write(html)

# Print context around every occurrence of "Balance" (case-insensitive) and around € amounts near it
for m in re.finditer(r'balance', html, re.IGNORECASE):
    start = max(0, m.start() - 150)
    end = min(len(html), m.end() + 150)
    snippet = re.sub(r'\s+', ' ', html[start:end])
    print("---")
    print(snippet)
