#!/usr/bin/env python3
"""The opposite of analyze_logs.py: LVP cards — the bottom N by win-rate lift.

For each deck, ranks the cards it wins LEAST with: the deck's win rate in
games where it cast the card, minus its overall win rate. A big negative
lift means the deck disproportionately loses when this card shows up —
a cut candidate.

Usage:
    python worst_cards.py                   # newest run under game_logs/
    python worst_cards.py game_logs/20260710_002406
    python worst_cards.py --min-games 5 --bottom 15
"""

import argparse
import os
import sys

from analyze_logs import analyze, newest_run_dir, parse_logs


def main():
    ap = argparse.ArgumentParser(description="Find LVP (worst-lift) cards in saved game logs.")
    ap.add_argument("run_dir", nargs="?", default=None,
                    help="Log directory (default: newest under game_logs/)")
    ap.add_argument("--min-games", type=int, default=None,
                    help="Minimum games a card must be cast in to rank "
                         "(default: adaptive — 5%% of the deck's games, min 3)")
    ap.add_argument("--bottom", type=int, default=10,
                    help="Bottom N cards per deck (default: 10)")
    ap.add_argument("--sort", choices=["winrate", "lift"], default="winrate",
                    help="Rank by raw win%% when cast, or by lift vs deck baseline "
                         "(default: winrate)")
    args = ap.parse_args()

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_logs")
    run_dir = args.run_dir or newest_run_dir(base)
    print(f"Analyzing: {run_dir}")

    games = parse_logs(run_dir)
    if not games:
        sys.exit("No completed games found in logs yet.")
    analyze(games, args.min_games, args.bottom, worst=True, sort_by=args.sort)


if __name__ == "__main__":
    main()
