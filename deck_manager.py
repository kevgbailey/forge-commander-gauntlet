import os
import re
import time
import glob as globmod

import requests

from models import Card, DeckList


def extract_deck_id(url: str) -> int:
    """Parse deck ID from an Archidekt URL like https://archidekt.com/decks/{id}/..."""
    match = re.search(r"archidekt\.com/decks/(\d+)", url)
    if not match:
        raise ValueError(f"Could not extract deck ID from URL: {url}")
    return int(match.group(1))


def fetch_archidekt_deck(deck_id: int, max_retries: int = 3) -> DeckList:
    """Fetch a deck from the Archidekt API and return a DeckList."""
    url = f"https://archidekt.com/api/decks/{deck_id}/"
    last_error = None

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  Retry {attempt + 1}/{max_retries} for deck {deck_id} in {wait}s...")
                time.sleep(wait)
    else:
        raise RuntimeError(f"Failed to fetch deck {deck_id} after {max_retries} attempts: {last_error}")

    deck_name = data.get("name", f"deck_{deck_id}")
    cards_main = []
    cards_sideboard = []
    cards_commander = []

    for card_entry in data.get("cards", []):
        categories = card_entry.get("categories", [])

        # Skip maybeboard cards
        if "Maybeboard" in categories:
            continue

        card_data = card_entry.get("card", {})
        oracle_card = card_data.get("oracleCard", {})
        edition = card_data.get("edition", {})

        name = oracle_card.get("name", "")
        if not name:
            continue

        set_code = edition.get("editioncode", "").upper()
        collector_number = card_data.get("collectorNumber", "")
        quantity = card_entry.get("quantity", 1)

        card = Card(
            quantity=quantity,
            name=name,
            set_code=set_code,
            collector_number=str(collector_number) if collector_number else "",
        )

        if "Commander" in categories:
            cards_commander.append(card)
        elif "Sideboard" in categories:
            cards_sideboard.append(card)
        else:
            cards_main.append(card)

    return DeckList(
        name=deck_name,
        cards_main=cards_main,
        cards_sideboard=cards_sideboard,
        cards_commander=cards_commander,
        archidekt_id=deck_id,
    )


def _format_card_line(card: Card) -> str:
    """Format a single card as a .dck line: {qty} {name}|{SET}|[{collector_number}]"""
    line = f"{card.quantity} {card.name}|{card.set_code}"
    if card.collector_number:
        line += f"|[{card.collector_number}]"
    return line


def deck_to_dck(deck: DeckList) -> str:
    """Convert a DeckList to Forge .dck file format string."""
    lines = []

    lines.append("[metadata]")
    lines.append(f"Name={deck.name}")

    if deck.cards_commander:
        lines.append("[Commander]")
        for card in deck.cards_commander:
            lines.append(_format_card_line(card))

    lines.append("[Main]")
    for card in deck.cards_main:
        lines.append(_format_card_line(card))

    if deck.cards_sideboard:
        lines.append("[Sideboard]")
        for card in deck.cards_sideboard:
            lines.append(_format_card_line(card))

    return "\n".join(lines) + "\n"


def _sanitize_filename(name: str) -> str:
    """Sanitize a deck name for use as a filename."""
    safe = re.sub(r"[^\w\s-]", "", name)
    safe = re.sub(r"\s+", "_", safe.strip())
    return safe[:80]


def save_deck(deck: DeckList, commander_dir: str) -> str:
    """Save a deck to Forge's commander deck directory. Returns the filename."""
    safe_name = _sanitize_filename(deck.name)
    filename = f"gauntlet_{safe_name}.dck"
    filepath = os.path.join(commander_dir, filename)

    os.makedirs(commander_dir, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(deck_to_dck(deck))

    return filename


def cleanup_gauntlet_decks(commander_dir: str) -> int:
    """Remove all gauntlet_*.dck files from the commander directory. Returns count removed."""
    pattern = os.path.join(commander_dir, "gauntlet_*.dck")
    files = globmod.glob(pattern)
    for f in files:
        os.remove(f)
    return len(files)
