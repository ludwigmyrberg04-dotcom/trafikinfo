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
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "0"))  # 0 = ett enda varv
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
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


# (sökord, emoji, färg) - först som matchar vinner
TYPES = [
    ("olycka med tungt", "🚛", 0xC0392B),
    ("olycka", "🚨", 0xE74C3C),
    ("avstängd", "⛔", 0xE74C3C),
    ("avstängning", "⛔", 0xE74C3C),
    ("brand", "🔥", 0xE74C3C),
    ("vägarbete", "🚧", 0xF39C12),
    ("bärgning", "🚛", 0x95A5A6),
    ("haveri", "🚗", 0x95A5A6),
    ("djur", "🦌", 0x2ECC71),
    ("olja", "🛢️", 0x7F8C8D),
    ("halka", "🧊", 0x5DADE2),
    ("snö", "❄️", 0x5DADE2),
    ("kö", "🚦", 0xE67E22),
    ("faror", "⚠️", 0xF1C40F),
    ("hinder", "⚠️", 0xF1C40F),
]


def classify(ev):
    text = " ".join([ev["title"], ev["kind"], ev["text"]]).lower()
    for word, emoji, color in TYPES:
        if word in text:
            return emoji, color
    return "📢", 0x3498DB


def make_embed(ev):
    emoji, color = classify(ev)
    status = ev["status"]
    status_emoji = "✅" if "avslutad" in status.lower() else "🔴"
    fields = []
    if ev["kind"]:
        fields.append({"name": "🏷️ Typ", "value": ev["kind"][:1024], "inline": True})
    if status:
        fields.append({"name": "📋 Status", "value": f"{status_emoji} {status}"[:1024], "inline": True})
    if ev["time"]:
        fields.append({"name": "🕒 Tid", "value": ev["time"][:1024], "inline": True})
    if ev["where"]:
        fields.append({"name": "📍 Plats", "value": ev["where"][:1024], "inline": False})
    return {
        "title": f"{emoji} {ev['title']}"[:256],
        "url": ev["url"],
        "color": color,
        "description": (ev["text"] or "—")[:4000],
        "fields": fields,
        "footer": {"text": "🚗 Källa: kollatrafiken.se"},
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


def run_once(state):
    """Ett varv. Returnerar True om det gick bra."""
    try:
        r = requests.get(URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        events = parse_events(r.text)
        problem = None if events else "Hittade inga händelser. Sidans HTML kan ha ändrats."
    except Exception as e:
        events, problem = [], f"Kunde inte hämta kollatrafiken.se ({e})"

    if problem:
        state["fails"] += 1
        print(problem, f"(misslyckade varv i rad: {state['fails']})")
        if state["fails"] == FAIL_ALERT_AFTER:
            send({"username": "Trafikinfo", "content": f"⚠️ Boten har problem: {problem}"})
        return False

    if state["fails"] >= FAIL_ALERT_AFTER:
        send({"username": "Trafikinfo", "content": "✅ Boten fungerar igen."})
    state["fails"] = 0

    if state["seen"] is None:  # första körningen: posta inget
        state["seen"] = [e["id"] for e in events]
        print(f"Första körningen: {len(events)} händelser markerade som sedda.")
        return True

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
    if new:
        print(f"Nya: {len(new)}, postade: {posted}")
    return True


def main():
    state = load_state()
    deadline = time.time() + LOOP_SECONDS
    while True:
        run_once(state)
        save_state({**state, "seen": state["seen"] or []})
        if time.time() + POLL_SECONDS >= deadline:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
