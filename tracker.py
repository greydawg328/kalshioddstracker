"""
Odds tracker.
Each run does two things:
  1. SNAPSHOT: for regular-season games starting in the next 45-120 minutes that
     haven't been logged yet, record Kalshi's prices for both teams.
  2. RESULTS: for logged games that should be over, fill in the final score/winner.

Data goes to data/games.csv. Prices are logged, never traded - no Kalshi account needed.
"""
import csv
import os
import re
from datetime import datetime, timedelta, timezone

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

# league: (Kalshi series ticker, ESPN sport path)
LEAGUES = {
    "NBA": ("KXNBAGAME", "basketball/nba"),
    "NHL": ("KXNHLGAME", "hockey/nhl"),
    "MLB": ("KXMLBGAME", "baseball/mlb"),
    "NFL": ("KXNFLGAME", "football/nfl"),
    "NCAAF": ("KXNCAAFGAME", "football/college-football"),
}

CSV_PATH = "data/games.csv"
FIELDS = [
    "league", "espn_id", "kalshi_event", "start_utc", "snapshot_utc", "mins_before",
    "away", "home",
    "away_bid", "away_ask", "away_last", "away_oi",
    "home_bid", "home_ask", "home_last", "home_oi",
    "away_score", "home_score", "winner",
]
WINDOW_MIN, WINDOW_MAX = 45, 120      # minutes before start
GIVE_UP_AFTER = timedelta(days=7)     # stop trying to fetch a result after this


def get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.url}")
            return None
        return r.json()
    except Exception as e:
        print(f"request failed: {url} ({e})")
        return None


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------- name matching between Kalshi and ESPN ----------
def norm(s):
    s = (s or "").lower()
    s = re.sub(r"\bst\.?(?=\s|$)", "state", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def kalshi_matches(kname, team):
    """Kalshi uses short names like 'Golden State', 'New York I', 'Arizona St.'.
    ESPN team objects have 'location' ('Golden State') and 'name' ('Warriors')."""
    k = norm(kname)
    loc = norm(team.get("location", ""))
    if k == loc:
        return True
    m = re.match(r"^(.*) ([a-z])$", k)  # 'new york i' -> Islanders
    return bool(m and m.group(1) == loc and norm(team.get("name", ""))[:1] == m.group(2))


# ---------- ESPN ----------
def espn_games(path, now):
    games = {}
    for offset in (-1, 0, 1):  # ESPN groups by US Eastern day, so look at 3 UTC days
        day = (now + timedelta(days=offset)).strftime("%Y%m%d")
        params = {"dates": day}
        if path.endswith("college-football"):
            params.update({"groups": 80, "limit": 300})
        data = get(f"{ESPN}/{path}/scoreboard", params)
        if not data:
            continue
        for ev in data.get("events", []):
            try:
                comp = ev["competitions"][0]
                t = {c["homeAway"]: c["team"] for c in comp["competitors"]}
                games[ev["id"]] = {
                    "id": ev["id"],
                    "start": parse_dt(ev["date"]),
                    "season_type": (ev.get("season") or {}).get("type"),
                    "state": ev["status"]["type"]["state"],
                    "home": t["home"],
                    "away": t["away"],
                }
            except (KeyError, IndexError):
                continue
    return list(games.values())


# ---------- Kalshi ----------
def kalshi_events(series):
    events, cursor = {}, None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get(f"{KALSHI}/markets", params)
        if not data:
            break
        for m in data.get("markets", []):
            events.setdefault(m["event_ticker"], []).append(m)
        cursor = data.get("cursor")
        if not cursor:
            break
    return events


def match_event(events, home, away):
    for ticker, mk in events.items():
        if len(mk) != 2:
            continue
        for h, a in ((mk[0], mk[1]), (mk[1], mk[0])):
            if kalshi_matches(h.get("yes_sub_title"), home) and kalshi_matches(a.get("yes_sub_title"), away):
                return ticker, h, a
    return None


# ---------- storage ----------
def load():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="") as f:
        return list(csv.DictReader(f))


def save(rows):
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


# ---------- the two jobs ----------
def snapshot(rows, now):
    seen = {(r["league"], r["espn_id"]) for r in rows}
    for league, (series, path) in LEAGUES.items():
        cands = []
        for g in espn_games(path, now):
            mins = (g["start"] - now).total_seconds() / 60
            if (
                g["state"] == "pre"
                and g["season_type"] in (2, None)  # 2 = regular season
                and (league, g["id"]) not in seen
                and WINDOW_MIN <= mins <= WINDOW_MAX
            ):
                cands.append((g, mins))
        if not cands:
            continue
        events = kalshi_events(series)
        for g, mins in cands:
            hit = match_event(events, g["home"], g["away"])
            if not hit:
                print(f"[{league}] no Kalshi match: {g['away']['displayName']} @ {g['home']['displayName']}")
                continue
            ticker, h, a = hit
            h_ask, a_ask = num(h.get("yes_ask_dollars")), num(a.get("yes_ask_dollars"))
            if not (h_ask and a_ask and 0 < h_ask < 1 and 0 < a_ask < 1):
                print(f"[{league}] no usable ask on both sides: {ticker}")
                continue
            rows.append({
                "league": league, "espn_id": g["id"], "kalshi_event": ticker,
                "start_utc": g["start"].isoformat(), "snapshot_utc": now.isoformat(),
                "mins_before": round(mins),
                "away": g["away"]["displayName"], "home": g["home"]["displayName"],
                "away_bid": a.get("yes_bid_dollars"), "away_ask": a.get("yes_ask_dollars"),
                "away_last": a.get("last_price_dollars"), "away_oi": a.get("open_interest_fp"),
                "home_bid": h.get("yes_bid_dollars"), "home_ask": h.get("yes_ask_dollars"),
                "home_last": h.get("last_price_dollars"), "home_oi": h.get("open_interest_fp"),
            })
            print(f"[{league}] logged {g['away']['displayName']} @ {g['home']['displayName']} ({round(mins)} min before)")


def results(rows, now):
    for r in rows:
        if r.get("winner"):
            continue
        start = parse_dt(r["start_utc"])
        if now < start + timedelta(hours=2) or now > start + GIVE_UP_AFTER:
            continue
        path = LEAGUES[r["league"]][1]
        data = get(f"{ESPN}/{path}/summary", {"event": r["espn_id"]})
        if not data:
            continue
        try:
            comp = data["header"]["competitions"][0]
            if not comp["status"]["type"]["completed"]:
                continue
            c = {x["homeAway"]: x for x in comp["competitors"]}
            hs, as_ = num(c["home"].get("score")), num(c["away"].get("score"))
            if hs is None or as_ is None:
                continue
            r["home_score"], r["away_score"] = int(hs), int(as_)
            r["winner"] = "home" if hs > as_ else "away" if as_ > hs else "tie"
            print(f"[{r['league']}] result: {r['away']} {int(as_)} - {int(hs)} {r['home']}")
        except (KeyError, IndexError):
            continue


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    rows = load()
    snapshot(rows, now)
    results(rows, now)
    save(rows)
