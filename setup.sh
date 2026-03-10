#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORGE_DIR="$SCRIPT_DIR/forge"
ENV_FILE="$SCRIPT_DIR/.env"

# ── Check prerequisites ──────────────────────────────────────────────

echo "Checking prerequisites..."

if ! command -v java &>/dev/null; then
    echo "ERROR: java not found. Install JDK 17+." >&2
    exit 1
fi

JAVA_VERSION=$(java -version 2>&1 | head -1 | sed 's/.*"\([0-9]*\).*/\1/')
if [ "$JAVA_VERSION" -lt 17 ] 2>/dev/null; then
    echo "ERROR: Java 17+ required, found version $JAVA_VERSION" >&2
    exit 1
fi
echo "  Java $JAVA_VERSION ✓"

if ! command -v mvn &>/dev/null; then
    echo "ERROR: mvn (Maven) not found." >&2
    exit 1
fi
echo "  Maven ✓"

# ── Build Forge ───────────────────────────────────────────────────────

echo "Building Forge (this may take a few minutes)..."
cd "$FORGE_DIR"
mvn -pl forge-gui-desktop -am package -DskipTests -q
cd "$SCRIPT_DIR"
echo "  Build complete ✓"

# ── Find the JAR ──────────────────────────────────────────────────────

JAR_PATH=$(find "$FORGE_DIR/forge-gui-desktop/target" \
    -name "forge-gui-desktop-*-jar-with-dependencies.jar" \
    -type f 2>/dev/null | head -1)

if [ -z "$JAR_PATH" ]; then
    echo "ERROR: Could not find forge-gui-desktop jar-with-dependencies.jar" >&2
    exit 1
fi
echo "  JAR: $JAR_PATH ✓"

# ── Detect OS and set commander deck dir ──────────────────────────────

OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Darwin)
        DECK_COMMANDER_DIR="$HOME/Library/Application Support/Forge/decks/commander/"
        ;;
    Linux)
        DECK_COMMANDER_DIR="$HOME/.forge/decks/commander/"
        ;;
    CYGWIN*|MINGW*|MSYS*)
        APPDATA="${APPDATA:-$HOME/AppData/Roaming}"
        DECK_COMMANDER_DIR="$APPDATA/Forge/decks/commander/"
        ;;
    *)
        echo "WARNING: Unknown OS '$OS_NAME', defaulting to ~/.forge/decks/commander/"
        DECK_COMMANDER_DIR="$HOME/.forge/decks/commander/"
        ;;
esac

mkdir -p "$DECK_COMMANDER_DIR"
echo "  Deck dir: $DECK_COMMANDER_DIR ✓"

# ── Write .env ────────────────────────────────────────────────────────

FORGE_GUI_DIR="$FORGE_DIR/forge-gui"

cat > "$ENV_FILE" <<ENVEOF
JAR_PATH=$JAR_PATH
FORGE_GUI_DIR=$FORGE_GUI_DIR
DECK_COMMANDER_DIR=$DECK_COMMANDER_DIR
MANDATORY_JAVA_ARGS=-Xmx4096m -Dio.netty.tryReflectionSetAccessible=true -Dfile.encoding=UTF-8
ADDOPEN_JAVA_ARGS=--add-opens java.desktop/java.beans=ALL-UNNAMED --add-opens java.desktop/javax.swing.border=ALL-UNNAMED --add-opens java.desktop/javax.swing.event=ALL-UNNAMED --add-opens java.desktop/sun.swing=ALL-UNNAMED --add-opens java.desktop/java.awt.image=ALL-UNNAMED --add-opens java.desktop/java.awt.color=ALL-UNNAMED --add-opens java.desktop/sun.awt.image=ALL-UNNAMED --add-opens java.desktop/javax.swing=ALL-UNNAMED --add-opens java.desktop/java.awt=ALL-UNNAMED --add-opens java.base/java.util=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED --add-opens java.base/java.lang.reflect=ALL-UNNAMED --add-opens java.base/java.text=ALL-UNNAMED --add-opens java.desktop/java.awt.font=ALL-UNNAMED --add-opens java.base/jdk.internal.misc=ALL-UNNAMED --add-opens java.base/sun.nio.ch=ALL-UNNAMED --add-opens java.base/java.nio=ALL-UNNAMED --add-opens java.base/java.math=ALL-UNNAMED --add-opens java.base/java.util.concurrent=ALL-UNNAMED --add-opens java.base/java.net=ALL-UNNAMED
ENVEOF

echo ""
echo "Setup complete! Configuration written to .env"
echo "Run: pip install -r requirements.txt"
