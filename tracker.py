"""
Odds tracker with three separate jobs. Run:  python tracker.py [radar|snapshot|results|all]
(no argument = all three, in this order)

  RADAR     Looks 24 hours ahead for regular-season games, matches each one to its two
            Kalshi markets, and saves the lineup to data/schedule.csv.
  SNAPSHOT  Checks the schedule for games starting in 45-120 minutes and reads the
            current prices for just those markets. Saves a row to data/games.csv.
  RESULTS   Fills in final scores and winners for logged games.

Prices are only read, never traded - no Kalshi account is needed.
"""
import csv
import os
import re
import sys
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

GAMES_PATH = "data/games.csv"
SCHEDULE_PATH = "data/schedule.csv"

GAME_FIELDS = [
    "league", "espn_id", "kalshi_event", "start_utc", "snapshot_utc", "mins_before",
    "away", "home",
    "away_bid", "away_ask", "away_last", "away_oi",
    "home_bid", "home_ask", "home_last", "home_oi",
    "away_score", "home_score", "winner",
]
SCHEDULE_FIELDS = [
    "league", "espn_id", "kalshi_event", "start_utc", "away", "home",
    "away_ticker", "home_ticker", "radar_utc",
]

RADAR_HOURS = 24                      # how far ahead the radar looks
WINDOW_MIN, WINDOW_MAX = 45, 120      # snapshot window, minutes before start
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
    for offset in (-1, 0, 1, 2):  # ESPN groups by US Eastern day, so look at several UTC days
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


def get_market(ticker):
    data = get(f"{KALSHI}/markets/{ticker}")
    if not data:
        return None
    return data.get("market", data)


# ---------- storage ----------
def load(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def save(path, fields, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


# ---------- job 1: RADAR ----------
def radar(schedule, games, now):
    logged = {(r["league"], r["espn_id"]) for r in games}
    by_key = {(r["league"], r["espn_id"]): r for r in schedule}
    for league, (series, path) in LEAGUES.items():
        upcoming = []
        for g in espn_games(path, now):
            hrs = (g["start"] - now).total_seconds() / 3600
            if g["state"] == "pre" and g["season_type"] in (2, None) and 0 < hrs <= RADAR_HOURS:
                upcoming.append(g)
        new = []
        for g in upcoming:
            key = (league, g["id"])
            if key in by_key:
                by_key[key]["start_utc"] = g["start"].isoformat()  # keep start time current
            elif key not in logged:
                new.append(g)
        print(f"[{league}] radar: {len(upcoming)} games in next {RADAR_HOURS}h, {len(new)} new")
        if not new:
            continue
        events = kalshi_events(series)
        for g in new:
            hit = match_event(events, g["home"], g["away"])
            if not hit:
                print(f"[{league}] no Kalshi match: {g['away']['displayName']} @ {g['home']['displayName']}")
                continue
            ticker, h, a = hit
            row = {
                "league": league, "espn_id": g["id"], "kalshi_event": ticker,
                "start_utc": g["start"].isoformat(),
                "away": g["away"]["displayName"], "home": g["home"]["displayName"],
                "away_ticker": a["ticker"], "home_ticker": h["ticker"],
                "radar_utc": now.isoformat(),
            }
            schedule.append(row)
            by_key[(league, g["id"])] = row
            print(f"[{league}] scheduled {row['away']} @ {row['home']}")
    # drop old rows so the file stays small
    return [r for r in schedule if parse_dt(r["start_utc"]) > now - timedelta(days=1)]


# ---------- job 2: SNAPSHOT ----------
def snapshot(games, schedule, now):
    seen = {(r["league"], r["espn_id"]) for r in games}
    due = []
    for s in schedule:
        if (s["league"], s["espn_id"]) in seen:
            continue
        mins = (parse_dt(s["start_utc"]) - now).total_seconds() / 60
        if WINDOW_MIN <= mins <= WINDOW_MAX:
            due.append((s, mins))
    print(f"snapshot: {len(schedule)} scheduled, {len(due)} due now")
    for s, mins in due:
        h, a = get_market(s["home_ticker"]), get_market(s["away_ticker"])
        if not h or not a:
            print(f"[{s['league']}] could not read markets: {s['kalshi_event']}")
            continue
        h_ask, a_ask = num(h.get("yes_ask_dollars")), num(a.get("yes_ask_dollars"))
        if not (h_ask and a_ask and 0 < h_ask < 1 and 0 < a_ask < 1):
            print(f"[{s['league']}] no usable ask on both sides: {s['kalshi_event']}")
            continue
        games.append({
            "league": s["league"], "espn_id": s["espn_id"], "kalshi_event": s["kalshi_event"],
            "start_utc": s["start_utc"], "snapshot_utc": now.isoformat(), "mins_before": round(mins),
            "away": s["away"], "home": s["home"],
            "away_bid": a.get("yes_bid_dollars"), "away_ask": a.get("yes_ask_dollars"),
            "away_last": a.get("last_price_dollars"), "away_oi": a.get("open_interest_fp"),
            "home_bid": h.get("yes_bid_dollars"), "home_ask": h.get("yes_ask_dollars"),
            "home_last": h.get("last_price_dollars"), "home_oi": h.get("open_interest_fp"),
        })
        print(f"[{s['league']}] logged {s['away']} @ {s['home']} ({round(mins)} min before)")


# ---------- job 3: RESULTS ----------
def results(games, now):
    for r in games:
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
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode not in ("radar", "snapshot", "results", "all"):
        sys.exit("usage: python tracker.py [radar|snapshot|results|all]")
    now = datetime.now(timezone.utc)
    games = load(GAMES_PATH)
    schedule = load(SCHEDULE_PATH)
    if mode in ("radar", "all"):
        schedule = radar(schedule, games, now)
    if mode in ("snapshot", "all"):
        snapshot(games, schedule, now)
    if mode in ("results", "all"):
        results(games, now)
    save(GAMES_PATH, GAME_FIELDS, games)
    save(SCHEDULE_PATH, SCHEDULE_FIELDS, schedule)
