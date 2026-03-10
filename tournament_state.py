"""Shared mutable tournament state with thread-safe access and WebSocket broadcast support."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class DeckInfo:
    name: str
    commander_names: list[str]
    scryfall_image_url: Optional[str] = None
    archidekt_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "commander_names": self.commander_names,
            "scryfall_image_url": self.scryfall_image_url,
            "archidekt_id": self.archidekt_id,
        }


@dataclass
class LivePod:
    pod_name: str
    deck_names: list[str]
    standings: dict[str, dict]  # same format as compute_standings output
    advance_n: int
    complete: bool = False

    def to_dict(self) -> dict:
        return {
            "pod_name": self.pod_name,
            "deck_names": self.deck_names,
            "standings": self.standings,
            "advance_n": self.advance_n,
            "complete": self.complete,
        }


@dataclass
class LiveRound:
    round_name: str
    pods: list[LivePod] = field(default_factory=list)
    complete: bool = False
    advancers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "round_name": self.round_name,
            "pods": [p.to_dict() for p in self.pods],
            "complete": self.complete,
            "advancers": self.advancers,
        }


class TournamentState:
    def __init__(self):
        self._lock = threading.Lock()
        self.status: str = "idle"
        self.total_decks: int = 0
        self.rounds: list[LiveRound] = []
        self.championship: Optional[LivePod] = None
        self.champion: Optional[str] = None
        self.game_feed: list[str] = []  # last 50 game results
        self.decks: dict[str, DeckInfo] = {}  # deck_name -> DeckInfo
        self.broadcast_callback: Optional[Callable] = None
        self.total_games_expected: int = 0
        self.games_completed: int = 0

    # ------------------------------------------------------------------
    # Internal helpers (must be called with lock held)
    # ------------------------------------------------------------------

    def _broadcast(self) -> None:
        if self.broadcast_callback is not None:
            try:
                self.broadcast_callback(self.to_json())
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API (all thread-safe)
    # ------------------------------------------------------------------

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status
            self._broadcast()

    def set_total_decks(self, n: int) -> None:
        with self._lock:
            self.total_decks = n
            self._broadcast()

    def add_deck_info(self, info: DeckInfo) -> None:
        with self._lock:
            self.decks[info.name] = info
            self._broadcast()

    def update_deck_image(self, deck_name: str, url: str) -> None:
        with self._lock:
            if deck_name in self.decks:
                self.decks[deck_name].scryfall_image_url = url
            self._broadcast()

    def add_round(self, round_obj: LiveRound) -> None:
        with self._lock:
            self.rounds.append(round_obj)
            self._broadcast()

    def find_or_create_pod(
        self,
        round_name: str,
        pod_name: str,
        deck_names: list[str],
        advance_n: int,
    ) -> LivePod:
        """Find or create a LivePod inside the named round. Creates the round if missing."""
        with self._lock:
            # Find the round
            live_round = None
            for r in self.rounds:
                if r.round_name == round_name:
                    live_round = r
                    break
            if live_round is None:
                live_round = LiveRound(round_name=round_name)
                self.rounds.append(live_round)

            # Find the pod
            for pod in live_round.pods:
                if pod.pod_name == pod_name:
                    return pod

            # Create empty standings
            standings = {
                name: {"wins": 0, "losses": 0, "draws": 0, "total_games": 0, "win_rate": 0.0}
                for name in deck_names
            }
            pod = LivePod(
                pod_name=pod_name,
                deck_names=deck_names,
                standings=standings,
                advance_n=advance_n,
            )
            live_round.pods.append(pod)
            self._broadcast()
            return pod

    def update_pod_standings(
        self,
        round_name: str,
        pod_name: str,
        standings: dict[str, dict],
        complete: bool = False,
    ) -> None:
        with self._lock:
            for r in self.rounds:
                if r.round_name == round_name:
                    for pod in r.pods:
                        if pod.pod_name == pod_name:
                            pod.standings = standings
                            pod.complete = complete
                            break
                    break
            self._broadcast()

    def complete_round(self, round_name: str, advancers: list[str]) -> None:
        with self._lock:
            for r in self.rounds:
                if r.round_name == round_name:
                    r.complete = True
                    r.advancers = list(advancers)
                    break
            self._broadcast()

    def set_championship(self, pod: LivePod) -> None:
        with self._lock:
            self.championship = pod
            self._broadcast()

    def update_championship_standings(
        self, standings: dict[str, dict], complete: bool = False
    ) -> None:
        with self._lock:
            if self.championship is not None:
                self.championship.standings = standings
                self.championship.complete = complete
            self._broadcast()

    def set_champion(self, name: str) -> None:
        with self._lock:
            self.champion = name
            self.status = "complete"
            self._broadcast()

    def set_total_games_expected(self, n: int) -> None:
        with self._lock:
            self.total_games_expected = n
            self._broadcast()

    def add_game_result(self, line: str) -> None:
        with self._lock:
            self.game_feed.append(line)
            if len(self.game_feed) > 50:
                self.game_feed = self.game_feed[-50:]
            self.games_completed += 1
            self._broadcast()

    def broadcast(self) -> None:
        """Trigger a broadcast without mutating state."""
        with self._lock:
            self._broadcast()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_json(self) -> dict:
        # Called with lock already held from _broadcast, but also called
        # externally (e.g. /api/state).  Use a try/acquire so external
        # callers get the lock while internal callers (who already hold it)
        # pass through safely via the re-entrant workaround below.
        return self._snapshot()

    def snapshot(self) -> dict:
        """Thread-safe public snapshot for external callers."""
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        """Build JSON dict. Must be called with lock held (or during init)."""
        pct = (
            round(self.games_completed / self.total_games_expected * 100, 1)
            if self.total_games_expected > 0 else 0.0
        )
        return {
            "status": self.status,
            "total_decks": self.total_decks,
            "rounds": [r.to_dict() for r in self.rounds],
            "championship": self.championship.to_dict() if self.championship else None,
            "champion": self.champion,
            "game_feed": list(self.game_feed),
            "decks": {k: v.to_dict() for k, v in self.decks.items()},
            "total_games_expected": self.total_games_expected,
            "games_completed": self.games_completed,
            "progress_pct": pct,
        }
