#!/usr/bin/env python3
"""
FPL Top-100 tracker.

Produces ONE markdown report combining:
  1. Top-100 (Overall league 314) squad ownership by position - last completed GW
  2. Top-100 captaincy split
  3. Top-100 net transfers made into the last completed GW
  4. LIVE global transfer movers for the upcoming GW (all managers)
  5. xG / xA / xGI for midfielders and forwards
  6. Biggest ownership gaps: top-100 vs global (where the elite are ahead of the crowd)
  7. DefCon leaderboard - threshold hits, not raw totals
Plus, with --team-id, a section 0 comparing your own squad against the elite.

Usage:
    python fpl_top100.py                 # fires only 5.5-8h before the next deadline
    python fpl_top100.py --deadlines     # print the full schedule and exit (no report)
    python fpl_top100.py --force         # build the report regardless of timing
    python fpl_top100.py --min-hours 5.5 --max-hours 8
    python fpl_top100.py --team-id 484852 # compare your own squad to the elite
    python fpl_top100.py --n 100         # how many managers to track
    python fpl_top100.py --email you@x.com   # also send via Resend (needs RESEND_API_KEY)

Output: ./reports/fpl_gw{N}.md  (and stdout)

Deadlines are never hardcoded. They are read live from FPL's bootstrap-static
endpoint on every run, so fixture changes and postponements are handled
automatically.

Duplicate protection: a scheduled run writes reports/.sent_gw{N} and any later
scheduled run for the same gameweek sees it and exits. Manual runs (--force)
skip both the timing window and the stamp, so you can test at any time without
blocking the real pre-deadline brief.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

API = "https://fantasy.premierleague.com/api"
OVERALL_LEAGUE = 314

# Defensive contribution ("DefCon") thresholds, 2026/27 rules.
# DEF: 10+ clearances, blocks, interceptions, tackles -> 2 pts.
# MID/FWD: 12+ of the same plus ball recoveries -> 2 pts. Capped at 2 per match.
DEFCON_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------- http

def get(url, tries=4, pause=0.35):
    """GET with retries. FPL 503s under load, especially near deadlines."""
    for attempt in range(tries):
        try:
            r = SESSION.get(url, timeout=25)
            if r.status_code == 200:
                time.sleep(pause)
                return r.json()
            if r.status_code == 404:
                return None
            time.sleep(2 ** attempt)
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def fnum(v, default=0.0):
    """FPL returns numbers as strings in places. Parse defensively."""
    try:
        return float(v)
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

    teams = {t["id"]: t.get("short_name", "?") for t in data.get("teams", [])}
    pos = {p["id"]: p.get("singular_name_short", "?") for p in data.get("element_types", [])}

    players = {}
    for e in data.get("elements", []):
        players[e["id"]] = {
            "id": e["id"],
            "name": e.get("web_name", "?"),
            "team": teams.get(e.get("team"), "?"),
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
        }

    events = data.get("events", [])
    finished = [e for e in events if e.get("data_checked") or e.get("finished")]
    last_gw = max((e["id"] for e in finished), default=None)

    nxt = next((e for e in events if e.get("is_next")), None)
    if nxt is None:
        nxt = next((e for e in events if not e.get("finished")), None)

    next_gw = nxt["id"] if nxt else None
    deadline = parse_dt(nxt.get("deadline_time")) if nxt else None

    return players, last_gw, next_gw, deadline, events


def print_deadlines(events, min_h, max_h):
    """Show every remaining deadline and the exact window the brief will fire in."""
    now = dt.datetime.now(dt.timezone.utc)
    print(f"{'GW':>3}  {'Deadline (UTC)':<20}  {'Brief window (UTC)':<30}  Status")
    print("-" * 86)
    for e in events:
        d = parse_dt(e.get("deadline_time"))
        if not d:
            continue
        start = d - dt.timedelta(hours=max_h)
        end = d - dt.timedelta(hours=min_h)
        if e.get("finished"):
            status = "done"
        elif d < now:
            status = "in progress"
        elif start <= now <= end:
            status = "*** WINDOW OPEN NOW ***"
        else:
            status = f"in {(d - now).days}d"
        print(f"{e['id']:>3}  {d.strftime('%a %d %b %H:%M'):<20}  "
              f"{start.strftime('%a %d %b %H:%M')} - {end.strftime('%H:%M'):<7}  {status}")
    print("\nDeadlines are read live from FPL on every run, not hardcoded.")


def load_defcon(last_gw, players):
    """
    Count DefCon threshold hits per player, from FPL's per-gameweek live endpoint.

    Totals are misleading because the award is capped at 2 points per match, so
    this counts how often a player actually cleared the threshold. One request
    per completed gameweek keeps it cheap all season.
    """
    stats = defaultdict(lambda: {"actions": 0, "hits": 0, "apps": 0})
    for gw in range(1, last_gw + 1):
        data = get(f"{API}/event/{gw}/live/")
        if not data:
            continue
        for el in data.get("elements", []):
            pid = el.get("id")
            s = el.get("stats", {})
            p = players.get(pid)
            if not p or p["pos"] not in DEFCON_THRESHOLD:
                continue
            if not s.get("minutes"):
                continue

            has_parts = any(k in s for k in
                            ("clearances_blocks_interceptions", "tackles", "recoveries"))
            if has_parts:
                cbi = s.get("clearances_blocks_interceptions", 0) or 0
                tck = s.get("tackles", 0) or 0
                rec = s.get("recoveries", 0) or 0
                # Recoveries count for MID/FWD only.
                actions = cbi + tck + (0 if p["pos"] == "DEF" else rec)
            else:
                actions = s.get("defensive_contribution", 0) or 0

            row = stats[pid]
            row["apps"] += 1
            row["actions"] += actions
            if actions >= DEFCON_THRESHOLD[p["pos"]]:
                row["hits"] += 1
    return stats


def defcon_cells(dc, pid):
    """Threshold hits and actions per appearance, as display strings."""
    d = dc.get(pid)
    if not d or not d["apps"]:
        return "0", "-"
    return str(d["hits"]), f"{d['actions'] / d['apps']:.1f}"


def load_my_squad(entry_id, gw):
    """The user's own locked squad for a completed gameweek. Public endpoint."""
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
    """Overall league 314. 50 entries per page."""
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


def load_manager_gw(entry_id, gw):
    """Returns (picks, captain_id, chip, transfers_in, transfers_out) for one manager."""
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


# ---------------------------------------------------------------- report

def line(p, extra=""):
    flag = ""
    if p["status"] != "a":
        flag = " ⚠️"
    elif p["chance"] is not None and p["chance"] < 100:
        flag = f" ({p['chance']}%)"
    return f"{p['name']} ({p['team']}) £{p['cost']:.1f}{flag}{extra}"


def build_report(players, top, last_gw, next_gw, deadline, squads, captains,
                 net_in, chips, dc, mine=None):
    n = len(top)
    out = []
    a = out.append

    hrs = ""
    if deadline:
        delta = deadline - dt.datetime.now(dt.timezone.utc)
        hrs = f" — {delta.total_seconds() / 3600:.1f}h to go"

    a(f"# FPL GW{next_gw} brief")
    a(f"**Deadline:** {deadline.strftime('%a %d %b %H:%M UTC') if deadline else 'unknown'}{hrs}")
    a(f"**Elite sample:** top {n} of Overall league (squads locked at GW{last_gw})")
    a(f"**Generated:** {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    a("")
    a("> Top-100 squads and transfers shown are for GW"
      f"{last_gw}. FPL hides GW{next_gw} squads until the deadline passes. "
      "Live global ownership, movers and xG below are current as of now.")
    a("")

    # 0. your squad
    if mine:
        a(f"## 0. Your squad vs the top {n}")
        bits = []
        if mine["rank"]:
            bits.append(f"OR {mine['rank']:,}")
        if mine["points"] is not None:
            bits.append(f"GW{last_gw}: {mine['points']} pts")
        bits.append(f"squad £{mine['value']:.1f}m, bank £{mine['bank']:.1f}m")
        if mine["chip"]:
            bits.append(f"chip: {mine['chip']}")
        a(" | ".join(bits))
        a("")

        a("| Player | Pos | Elite | Global | Edge | DefCon | xGI/90 |")
        a("|--------|-----|-------|--------|------|--------|--------|")
        order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
        for pid in sorted(mine["ids"],
                          key=lambda i: (order.get(players.get(i, {}).get("pos"), 9),
                                         -squads.get(i, 0))):
            p = players.get(pid)
            if not p:
                continue
            elite = 100 * squads.get(pid, 0) / n
            xi = "" if pid in mine["starting"] else " *(bench)*"
            cap = " **(C)**" if pid == mine["captain"] else ""
            hits, _ = defcon_cells(dc, pid)
            a(f"| {line(p)}{cap}{xi} | {p['pos']} | {elite:.0f}% | {p['owned']:.1f}% | "
              f"{elite - p['owned']:+.0f} | {hits} | {p['xgi90']:.2f} |")
        a("")

        owned = set(mine["ids"])
        missing = sorted(
            [(100 * c / n, pid) for pid, c in squads.items()
             if pid not in owned and 100 * c / n >= 30 and players.get(pid)],
            reverse=True)[:8]
        if missing:
            a(f"**Owned by the top {n}, missing from your squad**")
            a("")
            a("| Player | Pos | Elite | Global | Price | xGI/90 |")
            a("|--------|-----|-------|--------|-------|--------|")
            for elite, pid in missing:
                p = players[pid]
                a(f"| {line(p)} | {p['pos']} | {elite:.0f}% | {p['owned']:.1f}% | "
                  f"£{p['cost']:.1f} | {p['xgi90']:.2f} |")
            a("")

        risk = sorted(
            [(100 * squads.get(pid, 0) / n, net_in.get(pid, 0), pid)
             for pid in owned
             if players.get(pid) and 100 * squads.get(pid, 0) / n <= 10])
        if risk:
            a(f"**In your squad, largely avoided by the top {n}**")
            a("")
            a("| Player | Pos | Elite | Global | Their net move | xGI/90 |")
            a("|--------|-----|-------|--------|----------------|--------|")
            for elite, net, pid in risk[:8]:
                p = players[pid]
                a(f"| {line(p)} | {p['pos']} | {elite:.0f}% | {p['owned']:.1f}% | "
                  f"{net:+d} | {p['xgi90']:.2f} |")
            a("")

        if mine["captain"] and captains:
            top_cap, top_ct = captains.most_common(1)[0]
            mycap = players.get(mine["captain"])
            if mycap:
                if mine["captain"] == top_cap:
                    a(f"**Captain:** {mycap['name']} — same as {top_ct}% of the top {n}.")
                else:
                    tc = players.get(top_cap)
                    a(f"**Captain:** you had {mycap['name']}; "
                      f"{top_ct}% of the top {n} had "
                      f"{tc['name'] if tc else 'someone else'}.")
        a("")

    # 1. ownership by position
    a(f"## 1. Top-{n} ownership by position (GW{last_gw} locked squads)")
    for code, label, k in [("GKP", "Goalkeepers", 5),
                           ("DEF", "Defenders", 8),
                           ("MID", "Midfielders", 10),
                           ("FWD", "Forwards", 8)]:
        rows = [(pid, c) for pid, c in squads.most_common()
                if players.get(pid, {}).get("pos") == code][:k]
        if not rows:
            continue
        a(f"\n### {label}")
        if code == "DEF":
            a("| # | Player | Elite | Global | Gap | DefCon | Act/gm |")
            a("|---|--------|-------|--------|-----|--------|--------|")
        else:
            a("| # | Player | Elite | Global | Gap |")
            a("|---|--------|-------|--------|-----|")
        for i, (pid, c) in enumerate(rows, 1):
            p = players[pid]
            elite = 100 * c / n
            row = (f"| {i} | {line(p)} | {elite:.0f}% | {p['owned']:.1f}% | "
                   f"{elite - p['owned']:+.0f} |")
            if code == "DEF":
                hits, per = defcon_cells(dc, pid)
                row += f" {hits} | {per} |"
            a(row)
    a("")
    a("*DefCon = matches where the player hit the threshold and scored 2 points. "
      "Act/gm = average qualifying actions per appearance.*")
    a("")

    # 2. captaincy
    a(f"## 2. Top-{n} captaincy, GW{last_gw}")
    a("| Player | Count | Share |")
    a("|--------|-------|-------|")
    for pid, c in captains.most_common(6):
        p = players.get(pid)
        if p:
            a(f"| {line(p)} | {c} | {100 * c / max(sum(captains.values()), 1):.0f}% |")
    if chips:
        a("")
        a("**Chips played:** " + ", ".join(f"{k} ×{v}" for k, v in chips.most_common()))
    a("")

    # 3. elite transfers
    a(f"## 3. Top-{n} net transfers INTO GW{last_gw}")
    ins = sorted([(v, k) for k, v in net_in.items() if v > 0], reverse=True)[:12]
    outs = sorted([(v, k) for k, v in net_in.items() if v < 0])[:12]
    a("\n**Bought**")
    a("| Player | Net |")
    a("|--------|-----|")
    for v, pid in ins:
        if players.get(pid):
            a(f"| {line(players[pid])} | +{v} |")
    a("\n**Sold**")
    a("| Player | Net |")
    a("|--------|-----|")
    for v, pid in outs:
        if players.get(pid):
            a(f"| {line(players[pid])} | {v} |")
    a("")

    # 4. live global movers
    a(f"## 4. LIVE global movers into GW{next_gw} (all managers, updating now)")
    pool = [p for p in players.values() if p["tr_in"] or p["tr_out"]]
    a("\n**Most bought**")
    a("| Player | In | Out | Net | Owned |")
    a("|--------|----|-----|-----|-------|")
    for p in sorted(pool, key=lambda x: -x["tr_in"])[:12]:
        a(f"| {line(p)} | {p['tr_in']:,} | {p['tr_out']:,} | "
          f"{p['tr_in'] - p['tr_out']:+,} | {p['owned']:.1f}% |")
    a("\n**Most sold**")
    a("| Player | Out | In | Net | Owned |")
    a("|--------|-----|----|-----|-------|")
    for p in sorted(pool, key=lambda x: -x["tr_out"])[:12]:
        a(f"| {line(p)} | {p['tr_out']:,} | {p['tr_in']:,} | "
          f"{p['tr_in'] - p['tr_out']:+,} | {p['owned']:.1f}% |")
    a("")

    # 5. xG / xA
    a("## 5. xG / xA — midfielders and forwards (season to date)")
    for code, label in [("MID", "Midfielders"), ("FWD", "Forwards")]:
        pool = [p for p in players.values()
                if p["pos"] == code and p["minutes"] >= 180]
        pool.sort(key=lambda x: -x["xgi"])
        a(f"\n### {label} — top 15 by xGI")
        a("| Player | Mins | xG | xA | xGI | xGI/90 | Pts | Owned |")
        a("|--------|------|----|----|-----|--------|-----|-------|")
        for p in pool[:15]:
            a(f"| {line(p)} | {p['minutes']} | {p['xg']:.2f} | {p['xa']:.2f} | "
              f"{p['xgi']:.2f} | {p['xgi90']:.2f} | {p['points']} | {p['owned']:.1f}% |")
    a("")

    # 6. elite edge
    a(f"## 6. Where the top {n} are ahead of the crowd")
    a("Players the elite hold far more than the global field — the differentials that matter.")
    a("")
    a("| Player | Pos | Elite | Global | Gap | xGI/90 |")
    a("|--------|-----|-------|--------|-----|--------|")
    gaps = []
    for pid, c in squads.items():
        p = players.get(pid)
        if not p:
            continue
        elite = 100 * c / n
        gaps.append((elite - p["owned"], pid, elite))
    gaps.sort(reverse=True)
    for gap, pid, elite in gaps[:15]:
        p = players[pid]
        a(f"| {line(p)} | {p['pos']} | {elite:.0f}% | {p['owned']:.1f}% | "
          f"{gap:+.0f} | {p['xgi90']:.2f} |")
    a("")

    # 7. defcon leaderboard
    a("## 7. DefCon leaderboard")
    a("Defenders score 2 points for 10+ clearances, blocks, interceptions and tackles "
      "in a match. Midfielders and forwards need 12, with ball recoveries also "
      "counting. Capped at 2 points per match, so hits matter more than totals.")
    for code, label, thresh in [("DEF", "Defenders", 10),
                                ("MID", "Midfielders", 12),
                                ("FWD", "Forwards", 12)]:
        pool = [(d["hits"], d["actions"] / d["apps"], pid)
                for pid, d in dc.items()
                if d["apps"] and players.get(pid, {}).get("pos") == code]
        if not pool:
            continue
        pool.sort(reverse=True)
        a(f"\n### {label} — {thresh}+ actions per match")
        a("| Player | Hits | Apps | Act/gm | Global | Elite |")
        a("|--------|------|------|--------|--------|-------|")
        for hits, per, pid in pool[:12]:
            p = players[pid]
            elite = 100 * squads.get(pid, 0) / n
            a(f"| {line(p)} | {hits} | {dc[pid]['apps']} | {per:.1f} | "
              f"{p['owned']:.1f}% | {elite:.0f}% |")

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
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--min-hours", type=float, default=5.5,
                    help="Don't fire once the deadline is closer than this.")
    ap.add_argument("--max-hours", type=float, default=8.0,
                    help="Don't fire until the deadline is nearer than this.")
    ap.add_argument("--deadlines", action="store_true",
                    help="Print the deadline schedule and exit.")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--team-id", type=int,
                    help="Your own FPL entry id, to compare your squad against the elite.")
    ap.add_argument("--email")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    print("Loading bootstrap...", file=sys.stderr)
    players, last_gw, next_gw, deadline, events = load_bootstrap()

    if args.deadlines:
        print_deadlines(events, args.min_hours, args.max_hours)
        return

    if next_gw is None:
        sys.exit("No upcoming gameweek - season may be over.")

    path = Path(args.out) / f"fpl_gw{next_gw}.md"
    stamp = Path(args.out) / f".sent_gw{next_gw}"

    # Duplicate guard: one scheduled brief per gameweek, however often cron fires.
    # Manual runs use --force and never write the stamp, so testing whenever you
    # like can never block the real pre-deadline brief.
    if stamp.exists() and not args.force:
        print(f"GW{next_gw} brief already sent. Nothing to do.", file=sys.stderr)
        return

    if deadline and not args.force:
        hrs = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        if not (args.min_hours <= hrs <= args.max_hours):
            print(f"Deadline is {hrs:.1f}h away. Firing window is "
                  f"{args.min_hours}-{args.max_hours}h. Exiting.", file=sys.stderr)
            return

    if last_gw is None:
        sys.exit("No completed gameweek yet — nothing to analyse.")

    print(f"Last completed GW: {last_gw} | Next GW: {next_gw}", file=sys.stderr)
    print(f"Fetching top {args.n} managers...", file=sys.stderr)
    top = load_top_managers(args.n)
    if not top:
        sys.exit("Could not load league standings.")

    squads, captains, chips = Counter(), Counter(), Counter()
    net_in = defaultdict(int)

    for i, (rank, entry, name) in enumerate(top, 1):
        picks, cap, chip, t_in, t_out = load_manager_gw(entry, last_gw)
        squads.update(picks)
        if cap:
            captains[cap] += 1
        if chip:
            chips[chip] += 1
        for pid in t_in:
            net_in[pid] += 1
        for pid in t_out:
            net_in[pid] -= 1
        if i % 10 == 0:
            print(f"  {i}/{len(top)}", file=sys.stderr)

    print("Building DefCon history...", file=sys.stderr)
    dc = load_defcon(last_gw, players)

    mine = None
    if args.team_id:
        print(f"Loading your squad (entry {args.team_id})...", file=sys.stderr)
        mine = load_my_squad(args.team_id, last_gw)
        if not mine:
            print("Could not load your squad — carrying on without it.", file=sys.stderr)

    report = build_report(players, top, last_gw, next_gw, deadline,
                          squads, captains, net_in, chips, dc, mine)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    if not args.force:
        stamp.write_text(dt.datetime.now(dt.timezone.utc).isoformat(), encoding="utf-8")
    print(report)
    print(f"\nSaved to {path}", file=sys.stderr)

    if args.email:
        send_email(args.email, f"FPL GW{next_gw} brief — top {args.n} tracker", report)


if __name__ == "__main__":
    main()
