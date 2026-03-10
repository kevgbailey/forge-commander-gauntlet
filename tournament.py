#!/usr/bin/env python3
"""Tournament orchestrator for Forge MTG Commander AI-vs-AI simulations.

Usage:
    python tournament.py URL1 URL2 ... [--games 100] [--workers 4]
    python tournament.py --file decks.txt [--games 100]

Tournament format:
    - Decks are split into pods of 4 and play qualifying rounds
    - Top performers advance each round until 4 remain
    - Final 4 play the Championship
    - Every deck has a path to the Championship
"""

import argparse
import math
import random
import sys
import time

import requests
from dotenv import load_dotenv
from tabulate import tabulate

from deck_manager import (
    cleanup_gauntlet_decks,
    extract_deck_id,
    fetch_archidekt_deck,
    save_deck,
)
from engine import ForgeEngine
from models import PodMatchResult, PodResult
from tournament_state import DeckInfo, LivePod, TournamentState
from web_server import start_server

POD_SIZE = 4
CHAMPIONSHIP_SIZE = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Commander pod tournament using Forge AI simulations."
    )
    parser.add_argument(
        "urls",
        nargs="*",
        metavar="URL",
        help=f"Archidekt deck URLs (minimum {CHAMPIONSHIP_SIZE})",
    )
    parser.add_argument(
        "--file", "-f",
        type=str,
        default=None,
        help="Text file with one Archidekt URL per line",
    )
    parser.add_argument(
        "--games", "-n",
        type=int,
        default=100,
        help="Games per pod (default: 100)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=4,
        help="Max parallel JVM processes (default: 4, each uses ~4GB RAM)",
    )
    parser.add_argument(
        "--timeout", "-c",
        type=int,
        default=120,
        help="Per-game clock timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep gauntlet deck files after tournament",
    )
    return parser.parse_args()


def fetch_and_save_decks(
    urls: list[str], commander_dir: str
) -> list[tuple[str, str]]:
    """Fetch decks from Archidekt and save to Forge. Returns list of (filename, deck_name)."""
    decks = []
    for url in urls:
        try:
            deck_id = extract_deck_id(url)
            print(f"Fetching deck {deck_id} from Archidekt...")
            deck = fetch_archidekt_deck(deck_id)
            filename = save_deck(deck, commander_dir)
            print(f"  Saved: {filename} ({deck.name})")
            decks.append((filename, deck.name))
        except Exception as e:
            print(f"  ERROR fetching {url}: {e}")
            print(f"  Skipping this deck.")
    return decks


def group_into_pods(
    decks: list[tuple[str, str]]
) -> list[list[tuple[str, str]]]:
    """Split decks into pods of POD_SIZE. Last pod may be smaller."""
    pods = []
    for i in range(0, len(decks), POD_SIZE):
        pods.append(decks[i : i + POD_SIZE])
    return pods


def qualifiers_per_pod(num_pods: int) -> list[int]:
    """
    How many decks advance from each pod this round, distributed as evenly as
    possible so that exactly CHAMPIONSHIP_SIZE total advance.

    Used only for the final qualifying round (when num_pods <= CHAMPIONSHIP_SIZE).
    Earlier rounds always take top 1 per pod.

    Examples (CHAMPIONSHIP_SIZE = 4):
      2 pods → [2, 2]       8 decks → 4 finalists
      3 pods → [2, 1, 1]   12 decks → 4 finalists
      4 pods → [1, 1, 1, 1] 16 decks → 4 finalists
    """
    base = CHAMPIONSHIP_SIZE // num_pods
    extra = CHAMPIONSHIP_SIZE % num_pods
    return [base + (1 if i < extra else 0) for i in range(num_pods)]


def compute_standings(pod_match: PodMatchResult) -> dict[str, dict]:
    standings = {}
    for name in pod_match.deck_names:
        wins = pod_match.deck_wins[name]
        losses = pod_match.total_games - wins - pod_match.draws
        win_rate = wins / pod_match.total_games * 100 if pod_match.total_games > 0 else 0.0
        standings[name] = {
            "wins": wins,
            "losses": losses,
            "draws": pod_match.draws,
            "total_games": pod_match.total_games,
            "win_rate": win_rate,
        }
    return standings


def print_pod_results(pod_result: PodResult, advance_n: int = 0) -> None:
    """Print formatted results for a pod. advance_n decks are marked as advancing."""
    print(f"\n{'=' * 60}")
    print(f"=== {pod_result.pod_name} Results ===")
    print(f"{'=' * 60}")

    sorted_decks = sorted(
        pod_result.standings.items(),
        key=lambda x: x[1]["win_rate"],
        reverse=True,
    )

    table_data = []
    for rank, (name, s) in enumerate(sorted_decks, 1):
        marker = " →ADVANCE" if advance_n and rank <= advance_n else ""
        table_data.append([
            rank,
            name[:38] + marker,
            f"{s['win_rate']:.1f}%",
            s["wins"],
            s["losses"],
            s["draws"],
            s["total_games"],
        ])

    print(tabulate(
        table_data,
        headers=["Rank", "Deck Name", "Win Rate", "W", "L", "D", "Games"],
        tablefmt="simple",
    ))


def load_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls or [])
    if args.file:
        with open(args.file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    seen = set()
    unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def fetch_commander_image(commander_name: str) -> str | None:
    """Fetch the art_crop image URL for a commander from Scryfall."""
    try:
        resp = requests.get(
            "https://api.scryfall.com/cards/named",
            params={"fuzzy": commander_name},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            image_uris = data.get("image_uris", {})
            return image_uris.get("art_crop") or image_uris.get("normal")
    except Exception:
        pass
    return None


def run_round(
    round_name: str,
    decks: list[tuple[str, str]],
    engine: "ForgeEngine",
    num_games: int,
    clock_timeout: int,
    is_final_qualifying_round: bool,
    state: "TournamentState | None" = None,
) -> list[tuple[str, str]]:
    """
    Run one round of pods. Returns the list of (file, name) tuples that advance.

    If is_final_qualifying_round, takes enough per pod to produce exactly
    CHAMPIONSHIP_SIZE total. Otherwise takes top 1 from each pod.
    """
    pods = group_into_pods(decks)
    num_pods = len(pods)

    if is_final_qualifying_round:
        slots = qualifiers_per_pod(num_pods)
    else:
        slots = [1] * num_pods

    pod_specs = [
        ([f for f, _ in pod], [n for _, n in pod])
        for pod in pods
    ]

    print(f"\n{'=' * 60}")
    print(f"=== {round_name} ===")
    print(f"{'=' * 60}")
    for i, (_, names) in enumerate(pod_specs, 1):
        adv = slots[i - 1]
        print(f"  Pod {i} (top {adv} advance): {', '.join(names)}")
    print()

    if state is not None:
        state.set_status("running")

    pod_matches = engine.run_pods_parallel(
        pod_specs,
        num_games=num_games,
        clock_timeout=clock_timeout,
        state=state,
        round_name=round_name,
        advance_slots=slots,
    )

    name_to_match = {tuple(pm.deck_names): pm for pm in pod_matches}
    advancers: list[tuple[str, str]] = []

    for pod_idx, (pod_files, deck_names) in enumerate(pod_specs, 1):
        pod_match = name_to_match.get(tuple(deck_names))
        if pod_match is None:
            continue
        standings = compute_standings(pod_match)
        pod_result = PodResult(
            pod_name=f"{round_name} — Pod {pod_idx}",
            decks=deck_names,
            pod_match=pod_match,
            standings=standings,
        )
        advance_n = min(slots[pod_idx - 1], len(deck_names))
        print_pod_results(pod_result, advance_n=advance_n)

        sorted_names = sorted(
            standings.items(), key=lambda x: x[1]["win_rate"], reverse=True
        )
        for rank, (name, _) in enumerate(sorted_names):
            if rank < advance_n:
                file_idx = deck_names.index(name)
                advancers.append((pod_files[file_idx], name))

    if state is not None:
        state.complete_round(round_name, [n for _, n in advancers])

    return advancers


def count_total_games(num_decks: int, num_games: int) -> int:
    """Calculate total games that will be played across all rounds + championship."""
    total_pods = 0
    field = num_decks
    rounds = plan_rounds(num_decks)
    for i, _ in enumerate(rounds):
        num_pods = math.ceil(field / POD_SIZE)
        total_pods += num_pods
        is_final = (i == len(rounds) - 1)
        if is_final:
            slots = qualifiers_per_pod(num_pods)
            field = sum(min(s, POD_SIZE) for s in slots)
        else:
            field = num_pods
    total_pods += 1  # championship pod
    return total_pods * num_games


def plan_rounds(num_decks: int) -> list[str]:
    """
    Return the list of round names leading up to the Championship.
    Each round reduces the field by taking top 1 from each pod of 4,
    except the final qualifying round which takes enough per pod to
    fill CHAMPIONSHIP_SIZE spots.

    Examples:
      4  decks → []                        (go straight to championship)
      8  decks → ["Qualifying"]
      16 decks → ["Qualifying"]
      20 decks → ["Round 1", "Qualifying"]
      64 decks → ["Round 1", "Qualifying"]
      80 decks → ["Round 1", "Round 2", "Qualifying"]
    """
    round_names = ["Round 1", "Round 2", "Round 3", "Round 4", "Round 5"]
    rounds = []
    field = num_decks

    while True:
        num_pods = math.ceil(field / POD_SIZE)
        if num_pods <= CHAMPIONSHIP_SIZE:
            # This is the final qualifying round
            rounds.append("Qualifying Round")
            break
        # Intermediate round: top 1 per pod
        field = num_pods
        rounds.append(round_names[len(rounds)] if len(rounds) < len(round_names) else f"Round {len(rounds)+1}")

    return rounds


def run_tournament(args: argparse.Namespace) -> None:
    load_dotenv()
    engine = ForgeEngine(max_workers=args.workers)

    args.urls = load_urls(args)

    if len(args.urls) < CHAMPIONSHIP_SIZE:
        print(f"ERROR: Need at least {CHAMPIONSHIP_SIZE} deck URLs.")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # Start web server (non-blocking daemon thread)
    # ------------------------------------------------------------------ #
    state = TournamentState()
    port = 7777
    start_server(state, host="0.0.0.0", port=port)
    # Give uvicorn a moment to bind
    time.sleep(1.5)
    print(f"\n  Live UI: http://localhost:{port}\n")

    print(f"Tournament: {len(args.urls)} decks, {args.games} games/pod, "
          f"{args.workers} workers, {args.timeout}s timeout")
    print()

    # ------------------------------------------------------------------ #
    # Fetch decks
    # ------------------------------------------------------------------ #
    state.set_status("fetching")
    print("=== Fetching Decks ===")
    raw_decks = []
    for url in args.urls:
        try:
            deck_id = extract_deck_id(url)
            print(f"Fetching deck {deck_id} from Archidekt...")
            deck = fetch_archidekt_deck(deck_id)
            filename = save_deck(deck, engine.commander_dir)
            print(f"  Saved: {filename} ({deck.name})")
            raw_decks.append((filename, deck.name, deck))
        except Exception as e:
            print(f"  ERROR fetching {url}: {e}")
            print(f"  Skipping this deck.")

    decks = [(f, n) for f, n, _ in raw_decks]

    if len(decks) < CHAMPIONSHIP_SIZE:
        print(f"ERROR: Need at least {CHAMPIONSHIP_SIZE} successfully fetched decks.")
        sys.exit(1)

    # Populate deck info in state
    state.set_total_decks(len(decks))
    for filename, deck_name, deck in raw_decks:
        commander_names = [c.name for c in deck.cards_commander]
        info = DeckInfo(
            name=deck_name,
            commander_names=commander_names,
            archidekt_id=deck.archidekt_id,
        )
        state.add_deck_info(info)

    # Fetch Scryfall images (best-effort, non-blocking inline)
    print("\n=== Fetching Commander Art ===")
    for filename, deck_name, deck in raw_decks:
        commanders = deck.cards_commander
        if commanders:
            img_url = fetch_commander_image(commanders[0].name)
            if img_url:
                state.update_deck_image(deck_name, img_url)
                print(f"  Art fetched: {commanders[0].name}")
            else:
                print(f"  Art not found: {commanders[0].name}")
        # Scryfall rate-limit: 100ms between requests
        time.sleep(0.1)

    try:
        rounds = plan_rounds(len(decks))
        state.set_total_games_expected(count_total_games(len(decks), args.games))

        print(f"\nBracket: {len(decks)} decks → ", end="")
        field = len(decks)
        for i, rname in enumerate(rounds):
            num_pods = math.ceil(field / POD_SIZE)
            is_final = (i == len(rounds) - 1)
            if is_final:
                slots = qualifiers_per_pod(num_pods)
                advance = sum(slots)
            else:
                advance = num_pods  # top 1 per pod
            print(f"{rname} ({num_pods} pods) → {advance}", end=" → ")
            field = advance
        print(f"Championship")

        random.shuffle(decks)
        field_decks = decks
        for i, round_name in enumerate(rounds):
            is_final_qualifying = (i == len(rounds) - 1)
            random.shuffle(field_decks)
            field_decks = run_round(
                round_name,
                field_decks,
                engine,
                num_games=args.games,
                clock_timeout=args.timeout,
                is_final_qualifying_round=is_final_qualifying,
                state=state,
            )

        # Championship
        champ_files = [f for f, _ in field_decks]
        champ_names = [n for _, n in field_decks]

        print(f"\n{'=' * 60}")
        print(f"=== CHAMPIONSHIP ({len(champ_names)}-player) ===")
        print(f"{'=' * 60}")
        print(f"  {' vs '.join(champ_names)}")
        print()

        # Register championship pod in state
        champ_pod_standings = {
            name: {"wins": 0, "losses": 0, "draws": 0, "total_games": 0, "win_rate": 0.0}
            for name in champ_names
        }
        champ_live_pod = LivePod(
            pod_name="Championship",
            deck_names=champ_names,
            standings=champ_pod_standings,
            advance_n=1,
        )
        state.set_championship(champ_live_pod)

        champ_match = engine.run_pod_match(
            champ_files, champ_names,
            num_games=args.games,
            clock_timeout=args.timeout,
            state=state,
            round_name="Championship",
            pod_name="Championship",
            advance_n=1,
        )
        champ_standings = compute_standings(champ_match)
        champ_result = PodResult(
            pod_name="Championship",
            decks=champ_names,
            pod_match=champ_match,
            standings=champ_standings,
        )
        print_pod_results(champ_result)

        # Update championship standings and set champion
        state.update_championship_standings(champ_standings, complete=True)

        champion = max(champ_standings.items(), key=lambda x: x[1]["win_rate"])
        state.set_champion(champion[0])

        print(f"\n{'=' * 60}")
        print(f"  CHAMPION: {champion[0]} ({champion[1]['win_rate']:.1f}%)")
        print(f"{'=' * 60}")
        print(f"\n  Results: http://localhost:{port}\n")

    finally:
        if not args.no_cleanup:
            removed = cleanup_gauntlet_decks(engine.commander_dir)
            if removed:
                print(f"\nCleaned up {removed} gauntlet deck file(s).")


def main():
    args = parse_args()
    run_tournament(args)


if __name__ == "__main__":
    main()
