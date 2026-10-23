#!/usr/bin/env python3
"""Holt den Spielplan von handball4all und schreibt docs/spielplan.ics."""

import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# ===== Konfiguration =====
OG = "216"                       # Verband (Baden-Württembergischer HV)
CL = "164706"                    # Liga
CT = "1488636"                   # Team
TEAM_NAME = "TB Pforzheim 2"     # Sicherheitsfilter, "" = kein Filter
CAL_NAME = "Handball TB Pforzheim 2"
DURATION_MIN = 90                # Länge des Termins im Kalender
OUT = Path("docs/spielplan.ics")
STATE = Path("state.json")
TZ = ZoneInfo("Europe/Berlin")


def fetch():
    url = (f"https://spo.handball4all.de/service/if_g_json.php?ca=0&cl={CL}"
           f"&cmd=ps&ct={CT}&do={date.today():%Y-%m-%d}&og={OG}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "de-DE,de;q=0.9",
    }
    waits = [5, 15, 45, 90]
    raw = None
    for attempt in range(len(waits) + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as r:
                raw = r.read().decode("utf-8-sig").strip()
            break
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < len(waits):
                print(f"HTTP {e.code}, neuer Versuch in {waits[attempt]} s ...")
                time.sleep(waits[attempt])
                continue
            sys.exit(f"HTTP-Fehler {e.code} von handball4all")
        except URLError as e:
            if attempt < len(waits):
                print(f"Netzwerkfehler ({e.reason}), neuer Versuch in {waits[attempt]} s ...")
                time.sleep(waits[attempt])
                continue
            sys.exit(f"Netzwerkfehler: {e.reason}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
    root = data[0] if isinstance(data, list) and data else data
    if not isinstance(root, dict) or root.get("error"):
        sys.exit(f"handball4all meldet einen Fehler: {root!r:.300}")
    return root


def collect_games(root):
    content = root.get("content") or {}
    found = {}
    for key in ("futureGames", "actualGames", "lastGames"):
        part = content.get(key)
        if isinstance(part, dict):
            for g in part.get("games") or []:
                found[g.get("gID") or g.get("gNo") or json.dumps(g, sort_keys=True)] = g
    games = list(found.values())
    if not games:
        sys.exit("Keine Spiele in der Antwort gefunden.")
    if TEAM_NAME:
        n = TEAM_NAME.lower()
        filtered = [g for g in games
                    if n in str(g.get("gHomeTeam", "")).lower()
                    or n in str(g.get("gGuestTeam", "")).lower()]
        if not filtered:
            namen = sorted({str(g.get("gHomeTeam")) for g in games} |
                           {str(g.get("gGuestTeam")) for g in games})
            sys.exit(f'Kein Spiel mit "{TEAM_NAME}" gefunden. Teams in der Antwort: {namen}')
        games = filtered
    return games


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_time(s):
    m = re.match(r"^(\d{1,2}):(\d{2})", (s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def build_events(games):
    events = []
    for g in games:
        day = parse_date(g.get("gDate"))
        if not day:
            continue  # Spiel ohne Termin überspringen
        t = parse_time(g.get("gTime"))
        gid = str(g.get("gID") or g.get("gNo"))
        loc_parts = [
            (g.get("gGymnasiumName") or "").strip(),
            (g.get("gGymnasiumStreet") or "").strip(),
            f"{(g.get('gGymnasiumPostal') or '').strip()} {(g.get('gGymnasiumTown') or '').strip()}".strip(),
        ]
        desc = []
        hg, gg = str(g.get("gHomeGoals") or "").strip(), str(g.get("gGuestGoals") or "").strip()
        if hg and gg:
            desc.append(f"Ergebnis: {hg}:{gg}")
        ev = {
            "uid": gid,
            "summary": f"{g.get('gHomeTeam', '').strip()} vs. {g.get('gGuestTeam', '').strip()}",
            "location": ", ".join(p for p in loc_parts if p),
            "description": "\n".join(desc),
        }
        if t:
            start = datetime(day.year, day.month, day.day, t[0], t[1], tzinfo=TZ)
            end = start + timedelta(minutes=DURATION_MIN)
            ev["start"] = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ev["end"] = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ev["allday"] = False
        else:
            ev["start"] = day.strftime("%Y%m%d")
            ev["end"] = (day + timedelta(days=1)).strftime("%Y%m%d")
            ev["allday"] = True
        events.append(ev)
    events.sort(key=lambda e: e["start"])
    return events


def esc(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\n", "\\n"))


def fold(line):
    data = line.encode("utf-8")
    parts, limit = [], 75
    while len(data) > limit:
        cut = limit
        while (data[cut] & 0xC0) == 0x80:  # nicht mitten in einem UTF-8-Zeichen trennen
            cut -= 1
        parts.append(data[:cut].decode("utf-8"))
        data, limit = data[cut:], 74
    parts.append(data.decode("utf-8"))
    return "\r\n ".join(parts)


def write_ics(events, state):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//handball-kalender//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(CAL_NAME)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for ev in events:
        st = state[ev["uid"]]
        lines += ["BEGIN:VEVENT",
                  f"UID:{ev['uid']}@handball-kalender",
                  f"DTSTAMP:{st['stamp']}",
                  f"SEQUENCE:{st['seq']}"]
        if ev["allday"]:
            lines += [f"DTSTART;VALUE=DATE:{ev['start']}", f"DTEND;VALUE=DATE:{ev['end']}"]
        else:
            lines += [f"DTSTART:{ev['start']}", f"DTEND:{ev['end']}"]
        lines.append(f"SUMMARY:{esc(ev['summary'])}")
        if ev["location"]:
            lines.append(f"LOCATION:{esc(ev['location'])}")
        if ev["description"]:
            lines.append(f"DESCRIPTION:{esc(ev['description'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(l) for l in lines) + "\r\n")


def main():
    events = build_events(collect_games(fetch()))
    old = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state = {}
    for ev in events:
        sig = json.dumps(ev, sort_keys=True, ensure_ascii=False)
        prev = old.get(ev["uid"])
        if prev is None:
            state[ev["uid"]] = {"sig": sig, "seq": 0, "stamp": now}
        elif prev["sig"] != sig:
            # geänderter Termin: SEQUENCE erhöhen, damit Outlook die Änderung übernimmt
            state[ev["uid"]] = {"sig": sig, "seq": prev["seq"] + 1, "stamp": now}
        else:
            state[ev["uid"]] = prev
    write_ics(events, state)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")
    print(f"{len(events)} Spiele geschrieben nach {OUT}")


if __name__ == "__main__":
    main()
