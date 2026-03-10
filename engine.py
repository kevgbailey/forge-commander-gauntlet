import io
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Optional

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
    ):
        load_dotenv()
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
        draws = 0

        for line in stdout.splitlines():
            win_match = WIN_PATTERN.search(line)
            if win_match:
                game_num = int(win_match.group(1))
                duration = int(win_match.group(2))
                winner = _strip_ai_prefix(win_match.group(3))
                game_results.append(GameResult(game_num, winner, duration, is_draw=False))
                if winner in deck_wins:
                    deck_wins[winner] += 1
                continue

            draw_match = DRAW_PATTERN.search(line)
            if draw_match:
                game_num = int(draw_match.group(1))
                duration = int(draw_match.group(2))
                game_results.append(GameResult(game_num, None, duration, is_draw=True))
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
        )

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
    ) -> PodMatchResult:
        """Run N games of a pod (2–4+ decks playing simultaneously)."""
        cmd = self._build_command(deck_files, num_games, clock_timeout)
        process_timeout = num_games * clock_timeout + 120
        label = " vs ".join(deck_names)

        print(f"  Running pod: {label} ({num_games} games)...")

        if state is not None and round_name and pod_name:
            state.find_or_create_pod(round_name, pod_name, deck_names, advance_n)

        try:
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
            live_wins: dict[str, int] = {name: 0 for name in deck_names}
            live_draws = [0]
            live_games = [0]

            def _stream(src, buf):
                for line in src:
                    buf.write(line)

                    turn_match = TURN_PATTERN.search(line)
                    if turn_match:
                        last_turn[0] = int(turn_match.group(1))
                        continue

                    win_match = WIN_PATTERN.search(line)
                    if win_match:
                        turn_info = f" (turn {last_turn[0]})" if last_turn[0] else ""
                        feed_line = f"{line.rstrip()}{turn_info}"
                        print(f"  {feed_line}", flush=True)
                        if state is not None:
                            winner = _strip_ai_prefix(win_match.group(3))
                            if winner in live_wins:
                                live_wins[winner] += 1
                            live_games[0] += 1
                            standings = _compute_standings(
                                deck_names, live_wins, live_draws[0], live_games[0]
                            )
                            if round_name and pod_name:
                                state.update_pod_standings(round_name, pod_name, standings)
                            state.add_game_result(feed_line)
                        continue

                    draw_match = DRAW_PATTERN.search(line)
                    if draw_match:
                        turn_info = f" (turn {last_turn[0]})" if last_turn[0] else ""
                        feed_line = f"{line.rstrip()}{turn_info}"
                        print(f"  {feed_line}", flush=True)
                        if state is not None:
                            live_draws[0] += 1
                            live_games[0] += 1
                            standings = _compute_standings(
                                deck_names, live_wins, live_draws[0], live_games[0]
                            )
                            if round_name and pod_name:
                                state.update_pod_standings(round_name, pod_name, standings)
                            state.add_game_result(feed_line)

            t_out = threading.Thread(target=_stream, args=(proc.stdout, stdout_buf))
            t_err = threading.Thread(target=_stream, args=(proc.stderr, stderr_buf))
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=process_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                t_out.join()
                t_err.join()
                print(f"  TIMEOUT: pod exceeded {process_timeout}s")
                return PodMatchResult(
                    deck_names=deck_names,
                    deck_wins={name: 0 for name in deck_names},
                    draws=num_games,
                    total_games=num_games,
                )

            t_out.join()
            t_err.join()

            result = self._parse_pod_output(stdout_buf.getvalue(), deck_names, num_games)

            if state is not None and round_name and pod_name:
                final = _compute_standings(
                    deck_names, result.deck_wins, result.draws, result.total_games
                )
                state.update_pod_standings(round_name, pod_name, final, complete=True)

            return result

        except Exception as e:
            print(f"  ERROR launching pod: {e}")
            return PodMatchResult(
                deck_names=deck_names,
                deck_wins={name: 0 for name in deck_names},
                draws=num_games,
                total_games=num_games,
            )

    def run_pods_parallel(
        self,
        pods: list[tuple[list[str], list[str]]],
        num_games: int = 100,
        clock_timeout: int = 120,
        state: Optional["TournamentState"] = None,
        round_name: Optional[str] = None,
        advance_slots: Optional[list[int]] = None,
    ) -> list[PodMatchResult]:
        """Run multiple pod matches in parallel."""
        results = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_names = {}
            for pod_idx, (deck_files, deck_names) in enumerate(pods, 1):
                adv_n = advance_slots[pod_idx - 1] if advance_slots else 1
                p_name = f"{round_name} — Pod {pod_idx}" if round_name else f"Pod {pod_idx}"
                future = executor.submit(
                    self.run_pod_match,
                    deck_files, deck_names, num_games, clock_timeout,
                    state, round_name, p_name, adv_n,
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
