#!/bin/bash
# Start Pro DJ Link Bridge (Python)

cd "$(dirname "$0")"

echo "🎛️  Starting Pro DJ Link Bridge (Python)..."
echo ""

# Activate venv if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Check if python-prodj-link is available
if [ ! -d "python-prodj-link" ]; then
    echo "❌ python-prodj-link not found!"
    echo "   Run: git clone https://github.com/flesniak/python-prodj-link.git"
    exit 1
fi

# Start the bridge
exec python3 prodjlink_bridge.py
