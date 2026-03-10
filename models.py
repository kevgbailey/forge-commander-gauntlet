from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Card:
    quantity: int
    name: str
    set_code: str = ""
    collector_number: str = ""


@dataclass
class DeckList:
    name: str
    cards_main: list[Card] = field(default_factory=list)
    cards_sideboard: list[Card] = field(default_factory=list)
    cards_commander: list[Card] = field(default_factory=list)
    archidekt_id: Optional[int] = None


@dataclass
class GameResult:
    game_number: int
    winner: Optional[str]  # None for draws
    duration_ms: int
    is_draw: bool


@dataclass
class MatchResult:
    deck1_name: str
    deck2_name: str
    deck1_wins: int
    deck2_wins: int
    draws: int
    total_games: int
    game_results: list[GameResult] = field(default_factory=list)


@dataclass
class PodMatchResult:
    deck_names: list[str]
    deck_wins: dict[str, int]  # name -> win count
    draws: int
    total_games: int
    game_results: list[GameResult] = field(default_factory=list)


@dataclass
class PodResult:
    pod_name: str
    decks: list[str] = field(default_factory=list)
    pod_match: Optional[PodMatchResult] = None
    standings: dict[str, dict] = field(default_factory=dict)
