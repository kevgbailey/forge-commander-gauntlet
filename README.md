# Forge Gauntlet

An automated Commander AI tournament runner built on [Forge MTG](https://github.com/Card-Forge/forge). Feed it a list of Archidekt deck URLs and it runs a full multi-round bracket — qualifying pods, a championship game, and a live web UI with real-time standings, Scryfall card art, and a progress bar.

## How It Works

Decks are fetched from Archidekt and saved as Forge `.dck` files. Forge's headless `sim` mode runs the games as 40-life Commander matches with AI players. Results are parsed from stdout and fed into a live web dashboard over WebSocket.

### Tournament Format

All decks are distributed into **pods of 4** and play qualifying rounds. The bracket scales automatically based on how many decks you provide:

| Decks | Structure |
|-------|-----------|
| 4     | 1 pod → Championship |
| 8     | Qualifying (2 pods, top 2 each) → Championship |
| 16    | Qualifying (4 pods, top 1 each) → Championship |
| 20    | Round 1 (5 pods, top 1 each) → Qualifying (2 pods, top 2 each) → Championship |
| 35    | Round 1 (9 pods, top 1 each) → Qualifying (3 pods) → Championship |

- Pods are always 4 players (last pod may be 3 if deck count isn't divisible by 4)
- Pod assignments are randomized each round
- The Championship is always a 4-player game
- Every deck has a path to the Championship

## Prerequisites

- **Java 17+** — required to run Forge
- **Maven** — to build Forge from source
- **Python 3.11+**

## Setup

### 1. Clone this repo

```bash
git clone https://github.com/yourusername/forge-gauntlet.git
cd forge-gauntlet
```

### 2. Clone Forge into the `forge/` directory

```bash
git clone https://github.com/Card-Forge/forge.git forge
```

### 3. Build Forge and generate `.env`

```bash
bash setup.sh
```

This builds the Forge JAR from source and writes a `.env` file with the correct paths for your OS (macOS, Linux, or Windows/WSL).

> **First run:** Forge downloads card data on first launch (~1–2 GB). This happens automatically but can take a while.

### 4. Install Python dependencies

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

### With a deck list file

Create a text file with one Archidekt URL per line (lines starting with `#` are ignored):

```
# My tournament decks
https://archidekt.com/decks/12345/my_deck
https://archidekt.com/decks/67890/another_deck
```

Then run:

```bash
python tournament.py --file decks.txt
```

### With URLs directly

```bash
python tournament.py https://archidekt.com/decks/12345 https://archidekt.com/decks/67890 ...
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--file`, `-f` | — | Text file with one Archidekt URL per line |
| `--games`, `-n` | `100` | Games per pod |
| `--workers`, `-w` | `4` | Parallel JVM processes (each uses ~4 GB RAM) |
| `--timeout`, `-c` | `120` | Per-game clock timeout in seconds |
| `--no-cleanup` | — | Keep generated deck files after tournament |

### Example

```bash
python tournament.py --file decks.txt --games 20 --workers 2
```

## Live Web UI

When a tournament starts, a web server launches automatically at:

```
http://localhost:7777
```

The dashboard shows:

- **Progress bar** — games completed vs total games expected
- **Live bracket** — all rounds with pod standings updating after every game
- **Game feed** — live stream of game results with winner, turn count, and duration
- **Deck showcase** — commander card art pulled from Scryfall with win rate badges
- **Championship** — highlighted section with trophy and champion banner when complete

The UI updates in real time via WebSocket — no refresh needed.

## RAM Requirements

Each parallel JVM process uses ~4 GB of RAM. The default `--workers 4` requires ~16 GB. Reduce workers if you have less:

```bash
python tournament.py --file decks.txt --workers 2   # ~8 GB RAM
```

## Project Structure

```
forge-gauntlet/
├── tournament.py          # Main entry point — bracket logic, round orchestration
├── engine.py              # Forge JVM subprocess runner, output parser
├── tournament_state.py    # Thread-safe shared state for live UI updates
├── web_server.py          # FastAPI + WebSocket server
├── deck_manager.py        # Archidekt API fetcher, Forge .dck file writer
├── models.py              # Data classes (DeckList, PodMatchResult, etc.)
├── static/
│   └── index.html         # Self-contained live tournament UI
├── setup.sh               # Build Forge + generate .env
├── decks.txt              # Example deck list
├── requirements.txt
└── forge/                 # Forge MTG source (cloned separately, not committed)
```

## `.env` Reference

Generated automatically by `setup.sh`. You can edit it manually if needed:

```env
JAR_PATH=/path/to/forge-gui-desktop-*-jar-with-dependencies.jar
FORGE_GUI_DIR=/path/to/forge/forge-gui
DECK_COMMANDER_DIR=/path/to/Forge/decks/commander/
MANDATORY_JAVA_ARGS=-Xmx4096m ...
ADDOPEN_JAVA_ARGS=--add-opens java.desktop/...
```

| Variable | Description |
|----------|-------------|
| `JAR_PATH` | Path to the built Forge desktop JAR |
| `FORGE_GUI_DIR` | Forge GUI directory (needed for card data lookups) |
| `DECK_COMMANDER_DIR` | Where Forge looks for Commander decks |
| `MANDATORY_JAVA_ARGS` | JVM memory and encoding flags |
| `ADDOPEN_JAVA_ARGS` | Java module access flags required by Forge |

## Notes

- Decks must be public on Archidekt
- Only Commander-legal decks work (Forge validates the format)
- Forge's AI plays optimally within its heuristics — results reflect AI performance, not necessarily real-world power level
- Turn counts shown in game results are from the starting player's perspective (Forge limitation for multiplayer)
