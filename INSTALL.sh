#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JPSXDEC_DIR="$SCRIPT_DIR/jpsxdec"
JPSXDEC_ZIP="$SCRIPT_DIR/jpsxdec.zip"
JPSXDEC_URL="https://github.com/m35/jpsxdec/releases/download/v2.1/jpsxdec_v2.1-beta.zip"

echo "=== PSXmod installer ==="

# --- Java check ---
if ! command -v java &>/dev/null; then
    echo "ERROR: Java is not installed. Please install Java 8 or later."
    echo "  Ubuntu/Debian: sudo apt install default-jre"
    echo "  Fedora:        sudo dnf install java-latest-openjdk"
    exit 1
fi
echo "Java found: $(java -version 2>&1 | head -1)"

# --- Python deps ---
echo "Installing Python dependencies..."
pip install PyQt6 Pillow --break-system-packages --quiet
echo "Python dependencies installed."

# --- jPSXdec ---
if [ -f "$JPSXDEC_DIR/jpsxdec.jar" ]; then
    echo "jPSXdec already present, skipping download."
else
    echo "Downloading jPSXdec v2.1..."
    curl -L "$JPSXDEC_URL" -o "$JPSXDEC_ZIP"
    echo "Extracting..."
    mkdir -p "$JPSXDEC_DIR"
    unzip -q "$JPSXDEC_ZIP" -d "$JPSXDEC_DIR"
    # The zip puts files in a subdirectory; flatten it
    INNER=$(find "$JPSXDEC_DIR" -name "jpsxdec.jar" | head -1)
    if [ -z "$INNER" ]; then
        echo "ERROR: Could not find jpsxdec.jar in the downloaded archive."
        exit 1
    fi
    INNER_DIR="$(dirname "$INNER")"
    if [ "$INNER_DIR" != "$JPSXDEC_DIR" ]; then
        mv "$INNER_DIR"/* "$JPSXDEC_DIR"/
        rmdir "$INNER_DIR" 2>/dev/null || true
    fi
    rm -f "$JPSXDEC_ZIP"
    echo "jPSXdec installed to $JPSXDEC_DIR"
fi

echo ""
echo "=== Installation complete ==="
echo "Run with:  python3 $SCRIPT_DIR/main.py"
