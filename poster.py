import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
URL = "https://www.kollatrafiken.se/"
# Valfritt: kommaseparerade ord, t.ex. "Göteborg,Västra Götalands län"
KEYWORDS = [k.strip().lower() for k in os.getenv("FILTER_KEYWORDS", "").split(",") if k.strip()]
MAX_POSTS = 10
FAIL_ALERT_AFTER = 3  # larma efter så här många misslyckade körningar i rad
STATE = Path("state.json")
HEADERS = {"User-Agent": "KollaTrafikenDiscordPoster/1.0 (privat)"}
EVENT_RE = re.compile(r"/handelse/(\d+)")


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"seen": None, "fails": 0}


def save_state(state):
    state["seen"] = sorted(state["seen"], key=int)[-2000:]
    STATE.write_text(json.dumps(state))


def _event_block(h3):
    """Returnerar (länk, textrader) för rubriken, eller None om den inte är en händelse."""
    # 1) Klättra uppåt tills vi hittar en behållare med just den här rubriken och en händelselänk
    node = h3
    for _ in range(4):
        node = node.parent
        if node is None:
            break
        link = node.find("a", href=EVENT_RE)
        if link and len(node.find_all("h3")) == 1:
            lines = [l for l in node.stripped_strings]
            return link, lines[1:]  # hoppa över rubriken
    # 2) Annars: rubrik och innehåll är syskon på samma nivå
    lines, link = [], None
    for sib in h3.find_next_siblings():
        if sib.name == "h3":
            break
        lines.extend(sib.stripped_strings)
        link = sib if (sib.name == "a" and EVENT_RE.search(sib.get("href", ""))) \
            else sib.find("a", href=EVENT_RE)
        if link:
            break
    if link and link.find_previous("h3") is h3:
        return link, lines
    return None


def parse_events(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for h3 in soup.find_all("h3"):
        block = _event_block(h3)
        if not block:
            continue
        link, lines = block
        lines = [l for l in lines if not l.startswith("Läs mer")]
        href = link["href"]
        events.append({
            "id": EVENT_RE.search(href).group(1),
            "title": h3.get_text(" ", strip=True),
            "url": href if href.startswith("http") else "https://www.kollatrafiken.se" + href,
            "time": lines[0] if len(lines) > 0 else "",
            "status": lines[1] if len(lines) > 1 else "",
            "kind": lines[2] if len(lines) > 2 else "",
            "where": lines[3] if len(lines) > 3 else "",
            "text": " ".join(lines[4:]) if len(lines) > 4 else "",
        })
    return events


def make_embed(ev):
    lower = (ev["title"] + " " + ev["kind"]).lower()
    color = 0xE74C3C if "olycka" in lower else 0xF39C12 if "vägarbete" in lower else 0x3498DB
    fields = []
    for name, key, inline in [("Typ", "kind", True), ("Status", "status", True),
                              ("Tid", "time", True), ("Plats", "where", False)]:
        if ev[key]:
            fields.append({"name": name, "value": ev[key][:1024], "inline": inline})
    return {
        "title": ev["title"][:256],
        "url": ev["url"],
        "color": color,
        "description": (ev["text"] or "—")[:4000],
        "fields": fields,
        "footer": {"text": "Källa: kollatrafiken.se"},
    }


def send(payload):
    for _ in range(5):
        r = requests.post(WEBHOOK, json=payload, timeout=20)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord rate limit, gav upp")


def fail(state, message):
    state["fails"] += 1
    print(message, f"(misslyckade körningar i rad: {state['fails']})")
    if state["fails"] == FAIL_ALERT_AFTER:
        send({"username": "Trafikinfo", "content": f"⚠️ Boten har problem: {message}"})
    save_state({**state, "seen": state["seen"] or []})
    sys.exit(0)


def main():
    state = load_state()
    try:
        r = requests.get(URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        events = parse_events(r.text)
    except Exception as e:
        fail(state, f"Kunde inte hämta kollatrafiken.se ({e})")
    if not events:
        fail(state, "Hittade inga händelser. Sidans HTML kan ha ändrats.")

    if state["fails"] >= FAIL_ALERT_AFTER:
        send({"username": "Trafikinfo", "content": "✅ Boten fungerar igen."})
    state["fails"] = 0

    if state["seen"] is None:  # första körningen: posta inget
        state["seen"] = [e["id"] for e in events]
        save_state(state)
        print(f"Första körningen: {len(events)} händelser markerade som sedda.")
        return

    seen = set(state["seen"])
    new = [e for e in events if e["id"] not in seen]
    if KEYWORDS:
        to_post = [e for e in new if any(
            k in " ".join([e["title"], e["kind"], e["where"], e["text"]]).lower()
            for k in KEYWORDS)]
    else:
        to_post = new

    posted = 0
    for ev in list(reversed(to_post))[-MAX_POSTS:]:  # äldst först
        send({"username": "Trafikinfo", "embeds": [make_embed(ev)]})
        posted += 1
        time.sleep(1)

    seen.update(e["id"] for e in new)
    state["seen"] = list(seen)
    save_state(state)
    print(f"Nya: {len(new)}, postade: {posted}")


if __name__ == "__main__":
    main()
