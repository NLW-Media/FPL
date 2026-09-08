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

Usage:
    python fpl_top100.py                 # only runs inside the 6h-before-deadline window
    python fpl_top100.py --force         # run any time
    python fpl_top100.py --window 6      # change the window (hours before deadline)
    python fpl_top100.py --n 100         # how many managers to track
    python fpl_top100.py --email you@x.com   # also send via Resend (needs RESEND_API_KEY)

Output: ./reports/fpl_gw{N}.md  (and stdout)
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
    deadline = None
    if nxt and nxt.get("deadline_time"):
        deadline = dt.datetime.fromisoformat(nxt["deadline_time"].replace("Z", "+00:00"))

    return players, last_gw, next_gw, deadline


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


def build_report(players, top, last_gw, next_gw, deadline, squads, captains, net_in, chips):
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
        a("| # | Player | Elite | Global | Gap |")
        a("|---|--------|-------|--------|-----|")
        for i, (pid, c) in enumerate(rows, 1):
            p = players[pid]
            elite = 100 * c / n
            a(f"| {i} | {line(p)} | {elite:.0f}% | {p['owned']:.1f}% | "
              f"{elite - p['owned']:+.0f} |")
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
    ap.add_argument("--window", type=float, default=6.0,
                    help="Run only if deadline is this many hours away (±30 min).")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--email")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    print("Loading bootstrap...", file=sys.stderr)
    players, last_gw, next_gw, deadline = load_bootstrap()

    if deadline and not args.force:
        hrs = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        if not (args.window - 0.5 <= hrs <= args.window + 0.5):
            print(f"Deadline is {hrs:.1f}h away, outside the "
                  f"{args.window}h window. Exiting. Use --force to override.",
                  file=sys.stderr)
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

    report = build_report(players, top, last_gw, next_gw, deadline,
                          squads, captains, net_in, chips)

    Path(args.out).mkdir(parents=True, exist_ok=True)
    path = Path(args.out) / f"fpl_gw{next_gw}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nSaved to {path}", file=sys.stderr)

    if args.email:
        send_email(args.email, f"FPL GW{next_gw} brief — top {args.n} tracker", report)


if __name__ == "__main__":
    main()
