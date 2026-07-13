#!/usr/bin/env python3
"""Analyze saved Forge game logs for MVP cards.

For every completed game in a log directory, records which cards each deck
cast, who won, and how. A card's MVP score is the lift between the deck's
win rate in games where it cast that card vs. the deck's overall win rate.

Usage:
    python analyze_logs.py                  # newest run under game_logs/
    python analyze_logs.py game_logs/20260709_232705
    python analyze_logs.py --min-games 5 --top 15
"""

import argparse
import os
import re
import sys
from collections import defaultdict

CAST_PATTERN = re.compile(r"^Add To Stack: (.+?) cast (.+?)(?: targeting .*)?$")
WIN_PATTERN = re.compile(r"Game Result: Game \d+ ended in \d+ ms\. (.+) has won!")
DRAW_PATTERN = re.compile(r"Game Result: Game \d+ ended in a Draw!")
AI_PREFIX = re.compile(r"^Ai\(\d+\)-")
# e.g. "Ai(1)-deck a vs Ai(2)-deck b vs ... - ten games of Commander"
MATCHUP_PATTERN = re.compile(r"^(Ai\(\d+\)-.+ vs Ai\(\d+\)-.+?) - \w+.*games? of ")


def newest_run_dir(base: str) -> str:
    runs = sorted(
        (os.path.join(base, d) for d in os.listdir(base)),
        key=os.path.getmtime,
    )
    if not runs:
        sys.exit(f"No run directories in {base}")
    return runs[-1]


def parse_logs(run_dir: str):
    """Returns a list of games as (winner, {deck: set(cards cast)}, participants)."""
    games = []
    for fname in sorted(os.listdir(run_dir)):
        if not fname.endswith(".log"):
            continue
        casts: dict[str, set[str]] = defaultdict(set)
        participants: list[str] = []
        with open(os.path.join(run_dir, fname), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = MATCHUP_PATTERN.match(line)
                if m:
                    participants = [
                        AI_PREFIX.sub("", p.strip()) for p in m.group(1).split(" vs ")
                    ]
                    continue
                m = CAST_PATTERN.match(line)
                if m:
                    deck = AI_PREFIX.sub("", m.group(1))
                    casts[deck].add(m.group(2).strip())
                    continue
                m = WIN_PATTERN.search(line)
                if m:
                    games.append((AI_PREFIX.sub("", m.group(1)), dict(casts), list(participants)))
                    casts = defaultdict(set)
                    continue
                if DRAW_PATTERN.search(line):
                    games.append((None, dict(casts), list(participants)))
                    casts = defaultdict(set)
        # any partial game left in `casts` is still in progress — dropped
    return games


def analyze(games, min_games, top: int, worst: bool = False, sort_by: str = "lift"):
    deck_games: dict[str, int] = defaultdict(int)
    deck_wins: dict[str, int] = defaultdict(int)
    # card_stats[deck][card] = [games_cast, wins_when_cast]
    card_stats: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0])
    )

    for winner, casts, participants in games:
        # participants comes from the log header; fall back to casters
        for deck in (participants or casts):
            deck_games[deck] += 1
        if winner is not None:
            deck_wins[winner] += 1
        for deck, cards in casts.items():
            for card in cards:
                stat = card_stats[deck][card]
                stat[0] += 1
                if deck == winner:
                    stat[1] += 1

    total_games = len(games)
    print(f"Parsed {total_games} completed games.\n")

    for deck in sorted(deck_games, key=lambda d: (-deck_wins[d], d)):
        n = deck_games[deck]
        w = deck_wins[deck]
        baseline = w / n * 100 if n else 0.0
        # adaptive threshold: 5% of the deck's games, at least 3, unless
        # the user asked for a specific --min-games
        threshold = min_games if min_games is not None else max(3, round(n * 0.05))
        print(f"=== {deck} — {w}/{n} wins ({baseline:.1f}%) ===")

        if n < threshold:
            print(f"  (only {n} game{'s' if n != 1 else ''} so far — too few to rank cards)\n")
            continue

        rows = []
        for card, (cast_n, cast_w) in card_stats[deck].items():
            if cast_n < threshold:
                continue
            wr = cast_w / cast_n * 100
            rows.append((card, cast_n, cast_w, wr, wr - baseline))
        # r = (card, cast_n, cast_w, win_rate, lift)
        metric = 3 if sort_by == "winrate" else 4
        if worst:
            rows.sort(key=lambda r: (r[metric], -r[1]))
        else:
            rows.sort(key=lambda r: (-r[metric], -r[1]))

        if not rows:
            print(f"  (no card cast in >= {threshold} of this deck's games yet)\n")
            continue

        label = "LVP Card" if worst else "MVP Card"
        header = f"{label:<34} {'Cast in':>7} {'Won':>4} {'Win% when cast':>15} {'Lift':>6}"
        print(header)
        print("-" * len(header))
        for c, n_, w_, wr, lift in rows[:top]:
            print(f"{c[:34]:<34} {n_:>7} {w_:>4} {f'{wr:.0f}%':>15} {f'{lift:+.0f}%':>6}")
        print()


def main():
    ap = argparse.ArgumentParser(description="Find MVP cards in saved Forge game logs.")
    ap.add_argument("run_dir", nargs="?", default=None,
                    help="Log directory (default: newest under game_logs/)")
    ap.add_argument("--min-games", type=int, default=None,
                    help="Minimum games a card must be cast in to rank "
                         "(default: adaptive — 5%% of the deck's games, min 3)")
    ap.add_argument("--top", type=int, default=10,
                    help="Top N cards per deck (default: 10)")
    ap.add_argument("--sort", choices=["lift", "winrate"], default="lift",
                    help="Rank by lift vs deck baseline, or by raw win%% when cast (default: lift)")
    args = ap.parse_args()

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_logs")
    run_dir = args.run_dir or newest_run_dir(base)
    print(f"Analyzing: {run_dir}")

    games = parse_logs(run_dir)
    if not games:
        sys.exit("No completed games found in logs yet.")
    analyze(games, args.min_games, args.top, sort_by=args.sort)


if __name__ == "__main__":
    main()
