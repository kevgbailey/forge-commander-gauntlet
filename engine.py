import io
import math
import os
import random
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Callable, Optional

from dotenv import load_dotenv

from models import GameResult, PodMatchResult

if TYPE_CHECKING:
    from tournament_state import TournamentState

# Regex for parsing Forge output lines
WIN_PATTERN = re.compile(
    r"Game Result: Game (\d+) ended in (\d+) ms\. (.+) has won!"
)
DRAW_PATTERN = re.compile(
    r"Game Result: Game (\d+) ended in a Draw! Took (\d+) ms\."
)
TURN_PATTERN = re.compile(r"\bTurn (\d+)\b")
AI_PREFIX = re.compile(r"^Ai\(\d+\)-")

# GAME_OUTCOME log lines Forge prints for each player when a game ends
WON_BY_EFFECT = re.compile(r"has won due to effect of '(.+)'")
OPP_WON_SPELL = re.compile(r"has lost because an opponent has won by spell '(.+)'")
LOST_TO_SPELL = re.compile(r"has lost due to effect of spell '(.+)'")
LOSS_REASONS = [
    ("has lost because life total reached 0", "life to 0"),
    ("has lost due to accumulation of 21 damage from generals", "commander damage"),
    ("has lost trying to draw cards from empty library", "decked"),
    ("has lost because of obtaining 10 poison counters", "poison"),
    ("has conceded", "concession"),
]
OUTCOME_MARKERS = ("has lost", "has won due to effect", "has conceded")


def _is_outcome_line(line: str) -> bool:
    return any(marker in line for marker in OUTCOME_MARKERS)


def _classify_outcomes(outcome_lines: list[str]) -> str:
    """Summarize how a game ended from its GAME_OUTCOME log lines."""
    win_spell = None
    reasons: dict[str, int] = {}
    for line in outcome_lines:
        m = WON_BY_EFFECT.search(line)
        if m:
            win_spell = m.group(1)
            continue
        m = OPP_WON_SPELL.search(line)
        if m:
            win_spell = win_spell or m.group(1)
            continue
        m = LOST_TO_SPELL.search(line)
        if m:
            tag = f"spell: {m.group(1)}"
            reasons[tag] = reasons.get(tag, 0) + 1
            continue
        for needle, tag in LOSS_REASONS:
            if needle in line:
                reasons[tag] = reasons.get(tag, 0) + 1
                break
    if win_spell:
        return f"won via '{win_spell}'"
    if reasons:
        return ", ".join(
            f"{tag} ×{count}" if count > 1 else tag
            for tag, count in sorted(reasons.items(), key=lambda x: -x[1])
        )
    return ""


def _strip_ai_prefix(name: str) -> str:
    return AI_PREFIX.sub("", name)


def _compute_standings(
    deck_names: list[str],
    wins: dict[str, int],
    draws: int,
    total_games: int,
) -> dict[str, dict]:
    standings = {}
    for name in deck_names:
        w = wins.get(name, 0)
        losses = max(total_games - w - draws, 0)
        wr = w / total_games * 100 if total_games > 0 else 0.0
        standings[name] = {
            "wins": w,
            "losses": losses,
            "draws": draws,
            "total_games": total_games,
            "win_rate": wr,
        }
    return standings


class ForgeEngine:
    def __init__(
        self,
        jar_path: Optional[str] = None,
        commander_dir: Optional[str] = None,
        mandatory_args: Optional[str] = None,
        addopen_args: Optional[str] = None,
        max_workers: int = 4,
        log_dir: Optional[str] = None,
    ):
        load_dotenv()
        self.log_dir = log_dir
        if self.log_dir:
            os.makedirs(self.log_dir, exist_ok=True)
        self.jar_path = jar_path or os.environ["JAR_PATH"]
        self.commander_dir = commander_dir or os.environ["DECK_COMMANDER_DIR"]
        self.mandatory_args = mandatory_args or os.environ.get(
            "MANDATORY_JAVA_ARGS",
            "-Xmx4096m -Dio.netty.tryReflectionSetAccessible=true -Dfile.encoding=UTF-8",
        )
        self.addopen_args = addopen_args or os.environ.get("ADDOPEN_JAVA_ARGS", "")
        self.max_workers = max_workers
        self.forge_gui_dir = os.environ.get(
            "FORGE_GUI_DIR",
            os.path.join(os.path.dirname(os.path.abspath(self.jar_path)), "..", "..", "..", "forge-gui"),
        )

    def _build_command(self, deck_files: list[str], num_games: int, clock_timeout: int) -> list[str]:
        cmd = ["java"]
        cmd.extend(self.mandatory_args.split())
        if self.addopen_args:
            cmd.extend(self.addopen_args.split())
        cmd.extend(["-jar", self.jar_path, "sim"])
        cmd.extend(["-d"] + deck_files)
        cmd.extend(["-n", str(num_games)])
        cmd.extend(["-f", "Commander"])
        cmd.extend(["-c", str(clock_timeout)])
        return cmd

    def _parse_pod_output(self, stdout: str, deck_names: list[str], num_games: int) -> PodMatchResult:
        game_results = []
        deck_wins = {name: 0 for name in deck_names}
        win_methods: dict[str, dict[str, int]] = {name: {} for name in deck_names}
        draws = 0
        outcome_lines: list[str] = []

        for line in stdout.splitlines():
            if _is_outcome_line(line):
                outcome_lines.append(line)
                continue

            win_match = WIN_PATTERN.search(line)
            if win_match:
                game_num = int(win_match.group(1))
                duration = int(win_match.group(2))
                winner = _strip_ai_prefix(win_match.group(3))
                method = _classify_outcomes(outcome_lines)
                outcome_lines = []
                game_results.append(
                    GameResult(game_num, winner, duration, is_draw=False, win_method=method)
                )
                if winner in deck_wins:
                    deck_wins[winner] += 1
                    if method:
                        win_methods[winner][method] = win_methods[winner].get(method, 0) + 1
                continue

            draw_match = DRAW_PATTERN.search(line)
            if draw_match:
                game_num = int(draw_match.group(1))
                duration = int(draw_match.group(2))
                method = _classify_outcomes(outcome_lines)
                outcome_lines = []
                game_results.append(
                    GameResult(game_num, None, duration, is_draw=True, win_method=method)
                )
                draws += 1

        parsed_count = len(game_results)
        if parsed_count < num_games:
            missing = num_games - parsed_count
            draws += missing
            for i in range(missing):
                game_results.append(GameResult(parsed_count + i + 1, None, 0, is_draw=True))

        return PodMatchResult(
            deck_names=deck_names,
            deck_wins=deck_wins,
            draws=draws,
            total_games=num_games,
            game_results=game_results,
            win_methods=win_methods,
        )

    def _run_sim_shard(
        self,
        deck_files: list[str],
        deck_names: list[str],
        num_games: int,
        clock_timeout: int,
        shared: dict,
        lock: threading.Lock,
        state: Optional["TournamentState"] = None,
        round_name: Optional[str] = None,
        pod_name: Optional[str] = None,
        log_file: Optional[str] = None,
        on_game: Optional[Callable[[Optional[str], str], None]] = None,
    ) -> PodMatchResult:
        """Run one JVM simulating a share of a pod's games.

        If on_game is given it is called as on_game(winner_or_None, feed_line)
        per finished game and replaces the default state updates."""
        cmd = self._build_command(deck_files, num_games, clock_timeout)
        process_timeout = num_games * clock_timeout + 120

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.forge_gui_dir,
        )

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        last_turn = [0]
        # line-buffered so saved logs can be analyzed while games run
        log_fh = open(log_file, "w", encoding="utf-8", buffering=1) if log_file else None

        def _stream(src, buf, log=False):
            outcome_lines: list[str] = []
            for line in src:
                buf.write(line)
                if log and log_fh is not None:
                    log_fh.write(line)

                turn_match = TURN_PATTERN.search(line)
                if turn_match:
                    last_turn[0] = int(turn_match.group(1))
                    continue

                if _is_outcome_line(line):
                    outcome_lines.append(line)
                    continue

                win_match = WIN_PATTERN.search(line)
                draw_match = None if win_match else DRAW_PATTERN.search(line)
                if not win_match and not draw_match:
                    continue

                method = _classify_outcomes(outcome_lines)
                outcome_lines = []
                turn_info = f" (turn {last_turn[0]})" if last_turn[0] else ""
                method_info = f" — {method}" if method else ""
                feed_line = f"{line.rstrip()}{turn_info}{method_info}"
                print(f"  {feed_line}", flush=True)
                if on_game is not None:
                    winner = _strip_ai_prefix(win_match.group(3)) if win_match else None
                    on_game(winner, feed_line)
                    continue
                if state is None:
                    continue

                with lock:
                    if win_match:
                        winner = _strip_ai_prefix(win_match.group(3))
                        if winner in shared["wins"]:
                            shared["wins"][winner] += 1
                    else:
                        shared["draws"] += 1
                    shared["games"] += 1
                    standings = _compute_standings(
                        deck_names, shared["wins"], shared["draws"], shared["games"]
                    )
                if round_name and pod_name:
                    state.update_pod_standings(round_name, pod_name, standings)
                state.add_game_result(feed_line)

        t_out = threading.Thread(target=_stream, args=(proc.stdout, stdout_buf, True))
        t_err = threading.Thread(target=_stream, args=(proc.stderr, stderr_buf))
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=process_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"  TIMEOUT: shard exceeded {process_timeout}s")

        t_out.join()
        t_err.join()
        if log_fh is not None:
            log_fh.close()

        return self._parse_pod_output(stdout_buf.getvalue(), deck_names, num_games)

    def run_pod_match(
        self,
        deck_files: list[str],
        deck_names: list[str],
        num_games: int = 100,
        clock_timeout: int = 120,
        state: Optional["TournamentState"] = None,
        round_name: Optional[str] = None,
        pod_name: Optional[str] = None,
        advance_n: int = 1,
        shards: Optional[int] = None,
    ) -> PodMatchResult:
        """Run N games of a pod (2–4+ decks playing simultaneously).

        Games are split across `shards` parallel JVM processes (defaults to
        max_workers). Each JVM uses ~4 GB of RAM.
        """
        if shards is None:
            shards = self.max_workers
        shards = max(1, min(shards, num_games))
        label = " vs ".join(deck_names)

        print(f"  Running pod: {label} ({num_games} games across {shards} JVMs)...")

        if state is not None and round_name and pod_name:
            state.find_or_create_pod(round_name, pod_name, deck_names, advance_n)

        base = num_games // shards
        rem = num_games % shards
        counts = [base + (1 if i < rem else 0) for i in range(shards)]

        lock = threading.Lock()
        shared = {"wins": {name: 0 for name in deck_names}, "draws": 0, "games": 0}

        def _shard_log_file(shard_idx: int) -> Optional[str]:
            if not self.log_dir:
                return None
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", pod_name or " vs ".join(deck_names))
            return os.path.join(self.log_dir, f"{safe}_shard{shard_idx}.log")

        partials: list[PodMatchResult] = []
        try:
            with ThreadPoolExecutor(max_workers=shards) as executor:
                futures = [
                    executor.submit(
                        self._run_sim_shard,
                        deck_files, deck_names, count, clock_timeout,
                        shared, lock, state, round_name, pod_name,
                        _shard_log_file(idx),
                    )
                    for idx, count in enumerate(counts, 1) if count > 0
                ]
                for future in as_completed(futures):
                    partials.append(future.result())
        except Exception as e:
            print(f"  ERROR launching pod: {e}")
            return PodMatchResult(
                deck_names=deck_names,
                deck_wins={name: 0 for name in deck_names},
                draws=num_games,
                total_games=num_games,
            )

        deck_wins = {
            name: sum(p.deck_wins.get(name, 0) for p in partials)
            for name in deck_names
        }
        draws = sum(p.draws for p in partials)
        game_results = [gr for p in partials for gr in p.game_results]
        for i, gr in enumerate(game_results, 1):
            gr.game_number = i

        win_methods: dict[str, dict[str, int]] = {name: {} for name in deck_names}
        for p in partials:
            for name, methods in p.win_methods.items():
                for method, count in methods.items():
                    win_methods[name][method] = win_methods[name].get(method, 0) + count

        result = PodMatchResult(
            deck_names=deck_names,
            deck_wins=deck_wins,
            draws=draws,
            total_games=num_games,
            game_results=game_results,
            win_methods=win_methods,
        )

        if state is not None and round_name and pod_name:
            final = _compute_standings(
                deck_names, result.deck_wins, result.draws, result.total_games
            )
            state.update_pod_standings(round_name, pod_name, final, complete=True)

        return result

    def run_league(
        self,
        decks: list[tuple[str, str]],
        num_games: int = 1000,
        batch_size: int = 10,
        clock_timeout: int = 120,
        state: Optional["TournamentState"] = None,
    ) -> tuple[dict, dict, int]:
        """Shuffle-league mode: instead of a bracket, pods are re-randomized
        every round so each deck plays an even share of ~num_games total
        against a rotating field. Each pod batch is one JVM of batch_size
        games; batches run through the max_workers pool.

        Returns (standings, win_methods, total_games)."""
        names = [n for _, n in decks]
        pods_per_round = math.ceil(len(decks) / 4)
        games_per_round = pods_per_round * batch_size
        rounds = max(1, round(num_games / games_per_round))
        total_games = rounds * games_per_round

        schedule = []
        for r in range(1, rounds + 1):
            order = list(decks)
            random.shuffle(order)
            pods = [order[i : i + 4] for i in range(0, len(order), 4)]
            for p, pod in enumerate(pods, 1):
                if len(pod) >= 2:
                    schedule.append((r, p, pod))

        print(
            f"  Shuffle league: {len(decks)} decks, {rounds} rounds × "
            f"{pods_per_round} pods × {batch_size} games = {total_games} games "
            f"(~{total_games * 4 // len(decks)} per deck)"
        )

        round_label = "Shuffle League"
        pod_label = "League Standings"
        lock = threading.Lock()
        tally = {n: {"wins": 0, "draws": 0, "games": 0} for n in names}

        def league_standings() -> dict:
            return {
                n: {
                    "wins": t["wins"],
                    "losses": t["games"] - t["wins"] - t["draws"],
                    "draws": t["draws"],
                    "total_games": t["games"],
                    "win_rate": t["wins"] / t["games"] * 100 if t["games"] else 0.0,
                }
                for n, t in tally.items()
            }

        if state is not None:
            state.set_total_games_expected(total_games)
            state.find_or_create_pod(round_label, pod_label, names, advance_n=0)
            state.set_status("running")

        def make_on_game(pod_names: list[str]):
            def on_game(winner: Optional[str], feed_line: str) -> None:
                with lock:
                    for n in pod_names:
                        tally[n]["games"] += 1
                        if winner is None:
                            tally[n]["draws"] += 1
                    if winner in tally:
                        tally[winner]["wins"] += 1
                    standings = league_standings()
                if state is not None:
                    state.update_pod_standings(round_label, pod_label, standings)
                    state.add_game_result(feed_line)
            return on_game

        partials: list[PodMatchResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = []
            for r, p, pod in schedule:
                files = [f for f, _ in pod]
                pod_names = [n for _, n in pod]
                log_file = None
                if self.log_dir:
                    log_file = os.path.join(self.log_dir, f"league_r{r:03d}_p{p}.log")
                futures.append(executor.submit(
                    self._run_sim_shard,
                    files, pod_names, batch_size, clock_timeout,
                    {"wins": {n: 0 for n in pod_names}, "draws": 0, "games": 0},
                    threading.Lock(), None, None, None,
                    log_file, make_on_game(pod_names),
                ))
            for future in as_completed(futures):
                try:
                    partials.append(future.result())
                except Exception as e:
                    print(f"  ERROR in league batch: {e}")

        # Authoritative final tally from parsed shard results (covers games
        # the live stream may have missed, e.g. shard timeouts)
        final = {n: {"wins": 0, "draws": 0, "games": 0} for n in names}
        win_methods: dict[str, dict[str, int]] = {n: {} for n in names}
        for pm in partials:
            for n in pm.deck_names:
                if n in final:
                    final[n]["games"] += pm.total_games
                    final[n]["draws"] += pm.draws
                    final[n]["wins"] += pm.deck_wins.get(n, 0)
            for n, methods in pm.win_methods.items():
                for m, c in methods.items():
                    win_methods.setdefault(n, {})[m] = win_methods.get(n, {}).get(m, 0) + c

        with lock:
            tally.update(final)
            standings = league_standings()
        if state is not None:
            state.update_pod_standings(round_label, pod_label, standings, complete=True)

        return standings, win_methods, total_games

    def run_pods_parallel(
        self,
        pods: list[tuple[list[str], list[str]]],
        num_games: int = 100,
        clock_timeout: int = 120,
        state: Optional["TournamentState"] = None,
        round_name: Optional[str] = None,
        advance_slots: Optional[list[int]] = None,
    ) -> list[PodMatchResult]:
        """Run multiple pod matches in parallel.

        The max_workers JVM budget is split between concurrent pods; each pod
        further shards its games across its share of the budget.
        """
        results = []
        concurrent_pods = min(len(pods), self.max_workers)
        shards_per_pod = max(1, self.max_workers // max(1, concurrent_pods))

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_names = {}
            for pod_idx, (deck_files, deck_names) in enumerate(pods, 1):
                adv_n = advance_slots[pod_idx - 1] if advance_slots else 1
                p_name = f"{round_name} — Pod {pod_idx}" if round_name else f"Pod {pod_idx}"
                future = executor.submit(
                    self.run_pod_match,
                    deck_files, deck_names, num_games, clock_timeout,
                    state, round_name, p_name, adv_n, shards_per_pod,
                )
                future_to_names[future] = deck_names

            for future in as_completed(future_to_names):
                deck_names = future_to_names[future]
                try:
                    pod_result = future.result()
                    results.append(pod_result)
                    wins_str = ", ".join(
                        f"{n}: {pod_result.deck_wins[n]}W" for n in deck_names
                    )
                    print(f"  Done: {wins_str}, {pod_result.draws}D")
                except Exception as e:
                    print(f"  ERROR in pod {deck_names}: {e}")
                    results.append(
                        PodMatchResult(
                            deck_names=deck_names,
                            deck_wins={name: 0 for name in deck_names},
                            draws=num_games,
                            total_games=num_games,
                        )
                    )

        return results
