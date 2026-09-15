#!/usr/bin/env python3
"""
FPL Top-100 tracker and transfer engine.

Sections produced:
  0. Your squad vs the elite, with projected points
  0.5 RECOMMENDED TRANSFERS - ranked by projected gain over the next N gameweeks
  1. Elite ownership by position, split top-100 vs ranks 101+ (chip squads excluded)
  2. Captaincy and chip usage
  3. Elite net transfers (chip squads excluded)
  4. Live global transfer movers
  5. Form: rolling xG/xA over a recent window, plus minutes security
  6. Where the elite are ahead of the crowd
  7. DefCon leaderboard - threshold hits, not raw totals
  8. Fixture ticker

Chip handling: Free Hit and Wildcard squads are one-week constructions, so they
are excluded from ownership and transfer aggregates. Bench Boost and Triple
Captain squads are the manager's real team and are kept. Chip usage counts are
reported in full either way.

Usage:
    python fpl_top100.py                  # fires only 5.5-8h before the deadline
    python fpl_top100.py --deadlines      # print the schedule and exit
    python fpl_top100.py --force          # build regardless of timing
    python fpl_top100.py --team-id 484852 # your squad + transfer recommendations
    python fpl_top100.py --n 500          # sample size (default 500)
    python fpl_top100.py --workers 5      # parallel fetches
    python fpl_top100.py --horizon 4      # gameweeks to project over
    python fpl_top100.py --window 4       # gameweeks of form to average
    python fpl_top100.py --free-transfers 2
    python fpl_top100.py --email you@x.com

Output: ./reports/fpl_gw{N}.md
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

import requests

API = "https://fantasy.premierleague.com/api"
OVERALL_LEAGUE = 314

# Chips whose squads are not the manager's real team.
DISTORTING_CHIPS = {"freehit", "wildcard"}

# DefCon thresholds, 2026/27 rules. Capped at 2 pts per match.
DEFCON_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}

# FPL scoring.
GOAL_PTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
ASSIST_PTS = 3
CS_PTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}

# Clean-sheet probability by fixture difficulty rating (1 easiest, 5 hardest).
CS_PROB = {1: 0.45, 2: 0.36, 3: 0.27, 4: 0.18, 5: 0.12}
# Attacking output multiplier by fixture difficulty.
ATT_MULT = {1: 1.25, 2: 1.12, 3: 1.00, 4: 0.88, 5: 0.78}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
_LOCAL = threading.local()
RATE_LIMITED = threading.Event()


def session():
    """One requests.Session per thread - Session is not reliably thread-safe."""
    s = getattr(_LOCAL, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _LOCAL.session = s
    return s


# ---------------------------------------------------------------- http

def get(url, tries=5, pause=0.5):
    """
    GET with retries. Handles 429 explicitly: FPL rate-limits bursts from shared
    IPs like GitHub runners, and honouring Retry-After beats hammering it.
    """
    for attempt in range(tries):
        try:
            r = session().get(url, timeout=25)
            if r.status_code == 200:
                time.sleep(pause)
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                RATE_LIMITED.set()
                wait = 5.0
                try:
                    wait = float(r.headers.get("Retry-After", 5))
                except (TypeError, ValueError):
                    pass
                time.sleep(min(60.0, wait + 2 ** attempt))
                continue
            time.sleep(2 ** attempt)
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def fnum(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def inum(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_dt(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- data

def load_bootstrap():
    data = get(f"{API}/bootstrap-static/")
    if not data:
        sys.exit("Could not reach FPL bootstrap-static. Aborting.")

    teams = {}
    for t in data.get("teams", []):
        teams[t["id"]] = {
            "name": t.get("short_name", "?"),
            "att_h": t.get("strength_attack_home", 1000),
            "att_a": t.get("strength_attack_away", 1000),
            "def_h": t.get("strength_defence_home", 1000),
            "def_a": t.get("strength_defence_away", 1000),
        }
    pos = {p["id"]: p.get("singular_name_short", "?") for p in data.get("element_types", [])}

    players = {}
    for e in data.get("elements", []):
        players[e["id"]] = {
            "id": e["id"],
            "name": e.get("web_name", "?"),
            "team_id": e.get("team"),
            "team": teams.get(e.get("team"), {}).get("name", "?"),
            "pos": pos.get(e.get("element_type"), "?"),
            "cost": e.get("now_cost", 0) / 10,
            "owned": fnum(e.get("selected_by_percent")),
            "form": fnum(e.get("form")),
            "points": e.get("total_points", 0),
            "minutes": e.get("minutes", 0),
            "status": e.get("status", "a"),
            "chance": e.get("chance_of_playing_next_round"),
            "tr_in": e.get("transfers_in_event", 0),
            "tr_out": e.get("transfers_out_event", 0),
            "xg": fnum(e.get("expected_goals")),
            "xa": fnum(e.get("expected_assists")),
            "xgi": fnum(e.get("expected_goal_involvements")),
            "xg90": fnum(e.get("expected_goals_per_90")),
            "xa90": fnum(e.get("expected_assists_per_90")),
            "xgi90": fnum(e.get("expected_goal_involvements_per_90")),
            "pens": inum(e.get("penalties_order"), 0),
            "fks": inum(e.get("direct_freekicks_order"), 0),
            "corners": inum(e.get("corners_and_indirect_freekicks_order"), 0),
        }

    events = data.get("events", [])
    finished = [e for e in events if e.get("data_checked") or e.get("finished")]
    last_gw = max((e["id"] for e in finished), default=None)

    nxt = next((e for e in events if e.get("is_next")), None)
    if nxt is None:
        nxt = next((e for e in events if not e.get("finished")), None)

    next_gw = nxt["id"] if nxt else None
    deadline = parse_dt(nxt.get("deadline_time")) if nxt else None

    return players, teams, last_gw, next_gw, deadline, events


def load_fixtures(teams, from_gw, horizon):
    """Per-team list of upcoming fixtures: (gw, opponent_short, is_home, difficulty)."""
    data = get(f"{API}/fixtures/")
    if not data:
        return {}
    byteam = defaultdict(list)
    for f in data:
        gw = f.get("event")
        if gw is None or gw < from_gw or gw >= from_gw + horizon:
            continue
        if f.get("finished"):
            continue
        h, a = f.get("team_h"), f.get("team_a")
        hd = inum(f.get("team_h_difficulty"), 3)
        ad = inum(f.get("team_a_difficulty"), 3)
        if h in teams and a in teams:
            byteam[h].append((gw, teams[a]["name"], True, hd))
            byteam[a].append((gw, teams[h]["name"], False, ad))
    for t in byteam:
        byteam[t].sort()
    return dict(byteam)


def load_history(last_gw, players, window):
    """
    Per-gameweek stats from the live endpoint: DefCon, minutes, starts, xG, xA.
    One request per completed gameweek. Also builds a recent-form window.
    """
    hist = defaultdict(lambda: {"actions": 0, "hits": 0, "apps": 0, "gws": {}})
    first = max(1, last_gw - window + 1)
    for gw in range(1, last_gw + 1):
        data = get(f"{API}/event/{gw}/live/")
        if not data:
            continue
        for el in data.get("elements", []):
            pid = el.get("id")
            s = el.get("stats", {})
            p = players.get(pid)
            if not p:
                continue
            mins = inum(s.get("minutes"), 0)
            if not mins:
                continue

            row = hist[pid]
            row["gws"][gw] = {
                "minutes": mins,
                "starts": inum(s.get("starts"), 1 if mins >= 60 else 0),
                "xg": fnum(s.get("expected_goals")),
                "xa": fnum(s.get("expected_assists")),
            }

            if p["pos"] in DEFCON_THRESHOLD:
                has_parts = any(k in s for k in
                                ("clearances_blocks_interceptions", "tackles", "recoveries"))
                if has_parts:
                    cbi = inum(s.get("clearances_blocks_interceptions"), 0)
                    tck = inum(s.get("tackles"), 0)
                    rec = inum(s.get("recoveries"), 0)
                    actions = cbi + tck + (0 if p["pos"] == "DEF" else rec)
                else:
                    actions = inum(s.get("defensive_contribution"), 0)
                row["apps"] += 1
                row["actions"] += actions
                if actions >= DEFCON_THRESHOLD[p["pos"]]:
                    row["hits"] += 1

    # Roll up the recent window.
    played = max(1, last_gw - first + 1)
    for pid, row in hist.items():
        recent = [v for gw, v in row["gws"].items() if gw >= first]
        mins = sum(v["minutes"] for v in recent)
        row["r_mins"] = mins
        row["r_starts"] = sum(v["starts"] for v in recent)
        row["r_xg"] = sum(v["xg"] for v in recent)
        row["r_xa"] = sum(v["xa"] for v in recent)
        row["r_apps"] = len(recent)
        row["start_rate"] = min(1.0, row["r_starts"] / played)
        row["r_xg90"] = (row["r_xg"] / mins * 90) if mins else 0.0
        row["r_xa90"] = (row["r_xa"] / mins * 90) if mins else 0.0
        row["r_xgi90"] = row["r_xg90"] + row["r_xa90"]
        row["dc_rate"] = (row["hits"] / row["apps"]) if row["apps"] else 0.0
    return hist


def load_my_squad(entry_id, gw):
    data = get(f"{API}/entry/{entry_id}/event/{gw}/picks/")
    if not data:
        return None
    picks = data.get("picks", [])
    hist = data.get("entry_history") or {}
    return {
        "ids": [p["element"] for p in picks],
        "starting": [p["element"] for p in picks if p.get("position", 99) <= 11],
        "captain": next((p["element"] for p in picks if p.get("is_captain")), None),
        "chip": data.get("active_chip"),
        "bank": hist.get("bank", 0) / 10,
        "value": hist.get("value", 0) / 10,
        "rank": hist.get("overall_rank"),
        "points": hist.get("points"),
    }


def load_top_managers(n):
    ids, page = [], 1
    while len(ids) < n:
        data = get(f"{API}/leagues-classic/{OVERALL_LEAGUE}/standings/?page_standings={page}")
        if not data:
            break
        results = data.get("standings", {}).get("results", [])
        if not results:
            break
        for row in results:
            ids.append((row["rank"], row["entry"], row.get("player_name", "?")))
            if len(ids) >= n:
                break
        if not data.get("standings", {}).get("has_next"):
            break
        page += 1
    return ids[:n]


def fetch_all_managers(top, gw, workers):
    """
    Pull every manager's squad and transfers in parallel.
    Returns {entry_id: (picks, captain, chip, t_in, t_out)} plus a failure count.
    """
    results, failures = {}, 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(load_manager_gw, entry, gw): entry
                   for _, entry, _ in top}
        for fut in as_completed(futures):
            entry = futures[fut]
            done += 1
            try:
                picks, cap, chip, t_in, t_out = fut.result()
            except Exception:
                failures += 1
                continue
            if not picks:
                failures += 1
                continue
            results[entry] = (picks, cap, chip, t_in, t_out)
            if done % 50 == 0:
                print(f"  {done}/{len(top)} fetched", file=sys.stderr)
    return results, failures


def load_manager_gw(entry_id, gw):
    picks_data = get(f"{API}/entry/{entry_id}/event/{gw}/picks/")
    picks, captain, chip = [], None, None
    if picks_data:
        chip = picks_data.get("active_chip")
        for p in picks_data.get("picks", []):
            picks.append(p["element"])
            if p.get("is_captain"):
                captain = p["element"]

    t_in, t_out = [], []
    tdata = get(f"{API}/entry/{entry_id}/transfers/")
    if tdata:
        for t in tdata:
            if t.get("event") == gw:
                t_in.append(t["element_in"])
                t_out.append(t["element_out"])

    return picks, captain, chip, t_in, t_out


# ---------------------------------------------------------------- projection

def project(pid, players, hist, fixtures, horizon):
    """
    Expected FPL points over the next `horizon` gameweeks.

    Built from: start rate x rolling xG/xA per 90, scaled by fixture difficulty;
    clean-sheet probability from difficulty; DefCon hit rate; appearance points;
    a small bump for the designated penalty taker.
    Returns (total_points, detail_dict).
    """
    p = players.get(pid)
    if not p:
        return 0.0, {}
    h = hist.get(pid)
    if not h or not h.get("r_mins"):
        return 0.0, {"note": "no recent minutes"}

    # Availability.
    avail = 1.0
    if p["status"] != "a":
        avail = (p["chance"] / 100.0) if p["chance"] is not None else 0.0
    elif p["chance"] is not None:
        avail = p["chance"] / 100.0

    start_rate = h["start_rate"] * avail
    mins_per_gw = 90.0 * start_rate

    runs = fixtures.get(p["team_id"], [])[:horizon]
    if not runs:
        return 0.0, {"note": "no fixtures"}

    gpts = GOAL_PTS.get(p["pos"], 4)
    cs_pts = CS_PTS.get(p["pos"], 0)

    total = 0.0
    for gw, opp, home, diff in runs:
        mult = ATT_MULT.get(diff, 1.0)
        attack = (h["r_xg90"] * gpts + h["r_xa90"] * ASSIST_PTS) * (mins_per_gw / 90.0) * mult
        clean = CS_PROB.get(diff, 0.27) * cs_pts * start_rate
        defcon = h["dc_rate"] * 2.0 * start_rate
        appear = 2.0 * start_rate
        total += attack + clean + defcon + appear

    if p["pens"] == 1 and p["pos"] in ("MID", "FWD"):
        total += 0.35 * len(runs)

    return total, {
        "start_rate": start_rate,
        "xgi90": h["r_xgi90"],
        "dc_rate": h["dc_rate"],
        "fixtures": runs,
        "per_gw": total / max(1, len(runs)),
    }


def fixture_str(runs):
    if not runs:
        return "-"
    out = []
    for gw, opp, home, diff in runs:
        out.append(f"{opp.upper() if home else opp.lower()}({diff})")
    return " ".join(out)


def recommend(mine, players, hist, fixtures, horizon, bank, free_transfers, limit=6):
    """Rank every affordable same-position swap by projected gain over the horizon."""
    owned = set(mine["ids"])
    proj = {}
    for pid in players:
        proj[pid] = project(pid, players, hist, fixtures, horizon)[0]

    # Candidate pool: available, playing regularly, not already owned.
    pool = defaultdict(list)
    for pid, p in players.items():
        if pid in owned or p["status"] != "a":
            continue
        h = hist.get(pid)
        if not h or h.get("start_rate", 0) < 0.5:
            continue
        pool[p["pos"]].append(pid)
    for k in pool:
        pool[k].sort(key=lambda i: -proj[i])
        pool[k] = pool[k][:40]

    moves = []
    for out_id in owned:
        po = players.get(out_id)
        if not po:
            continue
        budget = po["cost"] + bank
        for in_id in pool.get(po["pos"], []):
            pi = players[in_id]
            if pi["cost"] > budget + 1e-9:
                continue
            gain = proj[in_id] - proj[out_id]
            if gain <= 0:
                continue
            moves.append((gain, out_id, in_id, budget - pi["cost"]))

    moves.sort(reverse=True)

    # Best combination for the transfers available, without reusing a player.
    chosen, used = [], set()
    running_bank = bank
    for gain, out_id, in_id, _ in moves:
        if len(chosen) >= max(1, free_transfers):
            break
        if out_id in used or in_id in used:
            continue
        cost = players[in_id]["cost"] - players[out_id]["cost"]
        if cost > running_bank + 1e-9:
            continue
        running_bank -= cost
        used.add(out_id)
        used.add(in_id)
        chosen.append((gain, out_id, in_id, running_bank))

    return moves[:limit], chosen, proj


# ---------------------------------------------------------------- report

def line(p, extra=""):
    flag = ""
    if p["status"] != "a":
        flag = " ⚠️"
    elif p["chance"] is not None and p["chance"] < 100:
        flag = f" ({p['chance']}%)"
    sp = ""
    if p["pens"] == 1:
        sp = " ᴾ"
    return f"{p['name']} ({p['team']}) £{p['cost']:.1f}{flag}{sp}{extra}"


def build_report(players, teams, coh, last_gw, next_gw, deadline,
                 captains, net_in, chips, hist, fixtures, horizon,
                 window, free_transfers, mine=None):
    """`coh` carries the two cohorts: top 100, ranks 101+, and their union."""
    out = []
    a = out.append

    squads = coh["all"]["squads"]
    n = max(1, coh["all"]["n"])
    n_t100 = max(1, coh["t100"]["n"])
    n_rest = max(1, coh["rest"]["n"])
    has_rest = coh["rest"]["n"] > 0

    def elite_pct(pid):
        return 100 * squads.get(pid, 0) / n

    def t100_pct(pid):
        return 100 * coh["t100"]["squads"].get(pid, 0) / n_t100

    def rest_pct(pid):
        return 100 * coh["rest"]["squads"].get(pid, 0) / n_rest

    def conviction(pid):
        """Flag players whose top-100 number isn't backed by the wider cohort."""
        if not has_rest:
            return ""
        d = t100_pct(pid) - rest_pct(pid)
        if d >= 20:
            return " ⚡"
        if d <= -20:
            return " ↓"
        return ""

    def cohort_cells(pid):
        if has_rest:
            return f"{t100_pct(pid):.0f}% | {rest_pct(pid):.0f}%"
        return f"{t100_pct(pid):.0f}% | -"

    hrs = ""
    if deadline:
        delta = deadline - dt.datetime.now(dt.timezone.utc)
        hrs = f" — {delta.total_seconds() / 3600:.1f}h to go"

    a(f"# FPL GW{next_gw} brief")
    a(f"**Deadline:** {deadline.strftime('%a %d %b %H:%M UTC') if deadline else 'unknown'}{hrs}")
    a(f"**Sample:** {coh['t100']['n']} of the top 100 and {coh['rest']['n']} "
      f"from ranks 101-{coh['requested']} — {n} real GW{last_gw} squads "
      f"(Free Hit and Wildcard excluded)")
    if coh["failures"]:
        a(f"**Incomplete:** {coh['failures']} manager(s) could not be fetched"
          + (" — FPL rate-limited this run" if coh["rate_limited"] else ""))
    a(f"**Form window:** last {window} GWs | **Projection horizon:** next {horizon} GWs")
    a(f"**Generated:** {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    a("")
    a(f"> Elite squads and transfers are GW{last_gw} — FPL hides GW{next_gw} teams until "
      "the deadline. Ownership, movers, xG and fixtures are current as of now. "
      "Fixture difficulty in brackets: 1 easiest, 5 hardest. UPPERCASE = home. "
      "ᴾ = first-choice penalty taker. T100 and Next are the two cohorts: "
      "⚡ means the top 100 are 20+ points keener than ranks 101+, so treat that "
      "figure as a small-sample artefact rather than consensus; ↓ is the reverse.")
    a("")

    # 0. your squad
    if mine:
        a(f"## 0. Your squad")
        bits = []
        if mine["rank"]:
            bits.append(f"OR {mine['rank']:,}")
        if mine["points"] is not None:
            bits.append(f"GW{last_gw}: {mine['points']} pts")
        bits.append(f"squad £{mine['value']:.1f}m, bank £{mine['bank']:.1f}m")
        bits.append(f"{free_transfers} FT")
        if mine["chip"]:
            bits.append(f"chip used: {mine['chip']}")
        a(" | ".join(bits))
        a("")
        a(f"| Player | Pos | Proj {horizon}GW | Start% | xGI/90 | DC | T100 | Next | Own | Fixtures |")
        a("|--------|-----|------|--------|--------|----|------|------|-----|----------|")
        order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
        rows = []
        for pid in mine["ids"]:
            p = players.get(pid)
            if not p:
                continue
            pts, det = project(pid, players, hist, fixtures, horizon)
            rows.append((order.get(p["pos"], 9), -pts, pid, pts, det))
        rows.sort()
        for _, _, pid, pts, det in rows:
            p = players[pid]
            h = hist.get(pid, {})
            cap = " **(C)**" if pid == mine["captain"] else ""
            bench = "" if pid in mine["starting"] else " *(b)*"
            a(f"| {line(p)}{cap}{bench}{conviction(pid)} | {p['pos']} | **{pts:.1f}** | "
              f"{100 * h.get('start_rate', 0):.0f}% | {h.get('r_xgi90', 0):.2f} | "
              f"{h.get('hits', 0)} | {cohort_cells(pid)} | {p['owned']:.1f}% | "
              f"{fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
        a("")

        # 0.5 recommendations
        top_moves, chosen, proj = recommend(
            mine, players, hist, fixtures, horizon, mine["bank"], free_transfers)

        a(f"## 0.5 Recommended transfers")
        if chosen:
            a(f"**Best use of your {free_transfers} free transfer"
              f"{'s' if free_transfers != 1 else ''}:**")
            a("")
            a("| Out | In | Gain | Bank after |")
            a("|-----|----|------|------------|")
            tot = 0.0
            for gain, out_id, in_id, left in chosen:
                tot += gain
                a(f"| {line(players[out_id])} | {line(players[in_id])} | "
                  f"**+{gain:.1f}** | £{left:.1f}m |")
            a("")
            a(f"Projected gain over {horizon} gameweeks: **+{tot:.1f} pts**. "
              f"A -4 hit needs +4.0 to break even.")
        else:
            a("No affordable swap projects a gain. Hold your transfer.")
        a("")

        if top_moves:
            a("**All best-value swaps considered**")
            a("")
            a("| Out | In | Gain | Spare |")
            a("|-----|----|------|-------|")
            for gain, out_id, in_id, left in top_moves:
                a(f"| {line(players[out_id])} | {line(players[in_id])} | "
                  f"+{gain:.1f} | £{left:.1f}m |")
            a("")

        # captain
        best_cap, best_pts = None, -1
        for pid in mine["starting"]:
            pts, _ = project(pid, players, hist, fixtures, 1)
            if pts > best_pts:
                best_cap, best_pts = pid, pts
        if best_cap:
            cp = players[best_cap]
            note = "matches your pick" if best_cap == mine["captain"] else \
                   f"you had {players.get(mine['captain'], {}).get('name', '?')}"
            a(f"**Captain for GW{next_gw}:** {cp['name']} "
              f"({best_pts:.1f} projected) — {note}.")
            if captains:
                tc, ct = captains.most_common(1)[0]
                a(f"The elite captained {players.get(tc, {}).get('name', '?')} "
                  f"in GW{last_gw} ({ct} of {n}, {100 * ct / n:.0f}%).")
        a("")

    # 1. ownership
    a(f"## 1. Elite ownership by position")
    a(f"Based on {n} real squads. T100 = top 100; Next = ranks 101-{coh['requested']}. "
      "Where the two disagree sharply, the wider cohort is the more reliable one.")
    for code, label, k in [("GKP", "Goalkeepers", 5), ("DEF", "Defenders", 8),
                           ("MID", "Midfielders", 10), ("FWD", "Forwards", 8)]:
        rows = [(pid, c) for pid, c in squads.most_common()
                if players.get(pid, {}).get("pos") == code][:k]
        if not rows:
            continue
        a(f"\n### {label}")
        a(f"| # | Player | T100 | Next | Own | Gap | Proj | Start% | DC | Fixtures |")
        a("|---|--------|------|------|-----|-----|------|--------|----|----------|")
        for i, (pid, c) in enumerate(rows, 1):
            p = players[pid]
            h = hist.get(pid, {})
            pts, _ = project(pid, players, hist, fixtures, horizon)
            a(f"| {i} | {line(p)}{conviction(pid)} | {cohort_cells(pid)} | "
              f"{p['owned']:.1f}% | {elite_pct(pid) - p['owned']:+.0f} | {pts:.1f} | "
              f"{100 * h.get('start_rate', 0):.0f}% | {h.get('hits', 0)} | "
              f"{fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
    a("")

    # 2. captaincy and chips
    a(f"## 2. Captaincy and chips, GW{last_gw}")
    a("| Player | Count | Share |")
    a("|--------|-------|-------|")
    for pid, c in captains.most_common(6):
        p = players.get(pid)
        if p:
            a(f"| {line(p)} | {c} | {100 * c / max(sum(captains.values()), 1):.0f}% |")
    a("")
    fetched = coh["fetched"]
    if chips:
        a(f"**Chips played** (of {fetched} managers fetched)")
        a("")
        a("| Chip | Managers | Share |")
        a("|------|----------|-------|")
        for k, v in chips.most_common():
            a(f"| {k} | {v} | {100 * v / max(1, fetched):.0f}% |")
        a("")
        a(f"{fetched - n} squad(s) excluded from the aggregates above "
          "(Free Hit and Wildcard only).")
    else:
        a(f"No chips played by the {fetched} managers fetched.")
    a("")

    # 3. elite transfers
    a(f"## 3. Elite net transfers INTO GW{last_gw}")
    a("Chip-squad transfers excluded — a Free Hit registers 15 moves that mean nothing.")
    ins = sorted([(v, k) for k, v in net_in.items() if v > 0], reverse=True)[:12]
    outs = sorted([(v, k) for k, v in net_in.items() if v < 0])[:12]
    a("\n**Bought**")
    a("| Player | Net | Proj | Fixtures |")
    a("|--------|-----|------|----------|")
    for v, pid in ins:
        p = players.get(pid)
        if p:
            pts, _ = project(pid, players, hist, fixtures, horizon)
            a(f"| {line(p)} | +{v} | {pts:.1f} | "
              f"{fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
    a("\n**Sold**")
    a("| Player | Net | Proj | Fixtures |")
    a("|--------|-----|------|----------|")
    for v, pid in outs:
        p = players.get(pid)
        if p:
            pts, _ = project(pid, players, hist, fixtures, horizon)
            a(f"| {line(p)} | {v} | {pts:.1f} | "
              f"{fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
    a("")

    # 4. live movers
    a(f"## 4. Live global movers into GW{next_gw}")
    pool = [p for p in players.values() if p["tr_in"] or p["tr_out"]]
    a("\n**Most bought**")
    a("| Player | Net | Own | Proj | Fixtures |")
    a("|--------|-----|-----|------|----------|")
    for p in sorted(pool, key=lambda x: -(x["tr_in"] - x["tr_out"]))[:12]:
        pts, _ = project(p["id"], players, hist, fixtures, horizon)
        a(f"| {line(p)} | {p['tr_in'] - p['tr_out']:+,} | {p['owned']:.1f}% | "
          f"{pts:.1f} | {fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
    a("\n**Most sold**")
    a("| Player | Net | Own | Proj | Fixtures |")
    a("|--------|-----|-----|------|----------|")
    for p in sorted(pool, key=lambda x: (x["tr_in"] - x["tr_out"]))[:12]:
        pts, _ = project(p["id"], players, hist, fixtures, horizon)
        a(f"| {line(p)} | {p['tr_in'] - p['tr_out']:+,} | {p['owned']:.1f}% | "
          f"{pts:.1f} | {fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
    a("")

    # 5. form
    a(f"## 5. Form — last {window} gameweeks")
    a("Rolling window, so a hot opening weekend stops flattering anyone. "
      "Start% is minutes security: the share of recent gameweeks started.")
    for code, label in [("MID", "Midfielders"), ("FWD", "Forwards"), ("DEF", "Defenders")]:
        rows = []
        for pid, h in hist.items():
            p = players.get(pid)
            if not p or p["pos"] != code or not h.get("r_mins"):
                continue
            if h["r_mins"] < 90:
                continue
            rows.append((h["r_xgi90"], pid))
        rows.sort(reverse=True)
        if not rows:
            continue
        a(f"\n### {label} — top 12 by recent xGI/90")
        a("| Player | Mins | xG/90 | xA/90 | xGI/90 | Start% | Proj | Own |")
        a("|--------|------|-------|-------|--------|--------|------|-----|")
        for _, pid in rows[:12]:
            p, h = players[pid], hist[pid]
            pts, _ = project(pid, players, hist, fixtures, horizon)
            a(f"| {line(p)} | {h['r_mins']} | {h['r_xg90']:.2f} | {h['r_xa90']:.2f} | "
              f"**{h['r_xgi90']:.2f}** | {100 * h['start_rate']:.0f}% | {pts:.1f} | "
              f"{p['owned']:.1f}% |")
    a("")

    # 6. elite edge
    a(f"## 6. Where the elite are ahead of the crowd")
    gaps = sorted(((elite_pct(pid) - players[pid]["owned"], pid)
                   for pid in squads if players.get(pid)), reverse=True)[:12]
    a("| Player | Pos | T100 | Next | Own | Gap | Proj | Fixtures |")
    a("|--------|-----|------|------|-----|-----|------|----------|")
    for gap, pid in gaps:
        p = players[pid]
        pts, _ = project(pid, players, hist, fixtures, horizon)
        a(f"| {line(p)}{conviction(pid)} | {p['pos']} | {cohort_cells(pid)} | "
          f"{p['owned']:.1f}% | {gap:+.0f} | {pts:.1f} | "
          f"{fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
    a("")

    # 7. defcon
    a("## 7. DefCon leaderboard")
    a("DEF need 10+ clearances, blocks, interceptions and tackles; MID and FWD "
      "need 12, recoveries included. Capped at 2 pts per match, so hits beat totals.")
    for code, label, thresh in [("DEF", "Defenders", 10), ("MID", "Midfielders", 12),
                                ("FWD", "Forwards", 12)]:
        rows = [(h["hits"], h["actions"] / h["apps"], pid)
                for pid, h in hist.items()
                if h.get("apps") and players.get(pid, {}).get("pos") == code]
        if not rows:
            continue
        rows.sort(reverse=True)
        a(f"\n### {label} — {thresh}+ per match")
        a("| Player | Hits | Apps | Act/gm | Own | Elite | Fixtures |")
        a("|--------|------|------|--------|-----|-------|----------|")
        for hits, per, pid in rows[:10]:
            p = players[pid]
            a(f"| {line(p)}{conviction(pid)} | {hits} | {hist[pid]['apps']} | {per:.1f} | "
              f"{p['owned']:.1f}% | {elite_pct(pid):.0f}% | "
              f"{fixture_str(fixtures.get(p['team_id'], [])[:horizon])} |")
    a("")

    # 8. fixture ticker
    a(f"## 8. Fixture ticker — next {horizon} gameweeks")
    a("Sorted by easiest run. Lower total difficulty is better.")
    a("")
    a("| Team | Total | Fixtures |")
    a("|------|-------|----------|")
    runs = []
    for tid, fl in fixtures.items():
        sl = fl[:horizon]
        if sl:
            runs.append((sum(d for _, _, _, d in sl) / len(sl), teams[tid]["name"], sl))
    runs.sort()
    for avg, name, sl in runs:
        a(f"| {name} | {avg:.1f} | {fixture_str(sl)} |")

    return "\n".join(out)


# ---------------------------------------------------------------- email

def send_email(to_addr, subject, body):
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        print("RESEND_API_KEY not set — skipping email.", file=sys.stderr)
        return
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps({
            "from": os.environ.get("FPL_FROM", "onboarding@resend.dev"),
            "to": [to_addr],
            "subject": subject,
            "text": body,
        }),
        timeout=20,
    )
    print("Email:", r.status_code, r.text[:200])


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500,
                    help="How many managers to sample from the Overall league.")
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel fetches. Raise carefully - FPL rate-limits bursts.")
    ap.add_argument("--min-hours", type=float, default=5.5)
    ap.add_argument("--max-hours", type=float, default=8.0)
    ap.add_argument("--horizon", type=int, default=4,
                    help="Gameweeks to project points over.")
    ap.add_argument("--window", type=int, default=4,
                    help="Gameweeks of form to average.")
    ap.add_argument("--free-transfers", type=int, default=1)
    ap.add_argument("--deadlines", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--team-id", type=int)
    ap.add_argument("--email")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    print("Loading bootstrap...", file=sys.stderr)
    players, teams, last_gw, next_gw, deadline, events = load_bootstrap()

    if args.deadlines:
        now = dt.datetime.now(dt.timezone.utc)
        print(f"{'GW':>3}  {'Deadline (UTC)':<20}  {'Brief window (UTC)':<30}  Status")
        print("-" * 86)
        for e in events:
            d = parse_dt(e.get("deadline_time"))
            if not d:
                continue
            start = d - dt.timedelta(hours=args.max_hours)
            end = d - dt.timedelta(hours=args.min_hours)
            if e.get("finished"):
                st = "done"
            elif d < now:
                st = "in progress"
            elif start <= now <= end:
                st = "*** WINDOW OPEN NOW ***"
            else:
                st = f"in {(d - now).days}d"
            print(f"{e['id']:>3}  {d.strftime('%a %d %b %H:%M'):<20}  "
                  f"{start.strftime('%a %d %b %H:%M')} - {end.strftime('%H:%M'):<7}  {st}")
        print("\nDeadlines are read live from FPL on every run, not hardcoded.")
        return

    if next_gw is None:
        sys.exit("No upcoming gameweek - season may be over.")

    path = Path(args.out) / f"fpl_gw{next_gw}.md"
    stamp = Path(args.out) / f".sent_gw{next_gw}"

    if stamp.exists() and not args.force:
        print(f"GW{next_gw} brief already sent. Nothing to do.", file=sys.stderr)
        return

    if deadline and not args.force:
        hrs = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        if not (args.min_hours <= hrs <= args.max_hours):
            print(f"Deadline is {hrs:.1f}h away. Window is "
                  f"{args.min_hours}-{args.max_hours}h. Exiting.", file=sys.stderr)
            return

    if last_gw is None:
        sys.exit("No completed gameweek yet — nothing to analyse.")

    print(f"Last completed GW: {last_gw} | Next GW: {next_gw}", file=sys.stderr)

    print("Loading fixtures...", file=sys.stderr)
    fixtures = load_fixtures(teams, next_gw, args.horizon)

    print(f"Fetching top {args.n} managers...", file=sys.stderr)
    top = load_top_managers(args.n)
    if not top:
        sys.exit("Could not load league standings.")

    print(f"Fetching {len(top)} squads with {args.workers} workers...", file=sys.stderr)
    fetched, failures = fetch_all_managers(top, last_gw, args.workers)

    captains, chips = Counter(), Counter()
    net_in = defaultdict(int)
    coh = {
        "t100": {"squads": Counter(), "n": 0},
        "rest": {"squads": Counter(), "n": 0},
        "all": {"squads": Counter(), "n": 0},
        "requested": len(top),
        "fetched": len(fetched),
        "failures": failures,
        "rate_limited": RATE_LIMITED.is_set(),
    }

    for rank, entry, name in top:
        row = fetched.get(entry)
        if not row:
            continue
        picks, cap, chip, t_in, t_out = row
        if chip:
            chips[chip] += 1
        # Free Hit and Wildcard squads are not that manager's real team.
        if chip in DISTORTING_CHIPS:
            continue

        bucket = "t100" if rank <= 100 else "rest"
        coh[bucket]["squads"].update(picks)
        coh[bucket]["n"] += 1
        coh["all"]["squads"].update(picks)
        coh["all"]["n"] += 1

        if cap:
            captains[cap] += 1
        for pid in t_in:
            net_in[pid] += 1
        for pid in t_out:
            net_in[pid] -= 1

    print(f"  {coh['all']['n']} real squads "
          f"({coh['t100']['n']} top-100, {coh['rest']['n']} rest), "
          f"{failures} failed", file=sys.stderr)

    print(f"Building history over {last_gw} gameweeks...", file=sys.stderr)
    hist = load_history(last_gw, players, args.window)

    mine = None
    if args.team_id:
        print(f"Loading your squad (entry {args.team_id})...", file=sys.stderr)
        mine = load_my_squad(args.team_id, last_gw)
        if not mine:
            print("Could not load your squad — carrying on without it.", file=sys.stderr)

    report = build_report(players, teams, coh, last_gw, next_gw,
                          deadline, captains, net_in, chips, hist,
                          fixtures, args.horizon, args.window,
                          args.free_transfers, mine)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    if not args.force:
        stamp.write_text(dt.datetime.now(dt.timezone.utc).isoformat(), encoding="utf-8")
    print(report)
    print(f"\nSaved to {path}", file=sys.stderr)

    if args.email:
        send_email(args.email, f"FPL GW{next_gw} brief", report)


if __name__ == "__main__":
    main()
