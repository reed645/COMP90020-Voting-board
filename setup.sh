#!/bin/bash
set -e

echo "=== Setting up Distributed Voting System ==="

# Create virtual environment if not exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate and install
echo "Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

echo ""
echo "=== Setup complete ==="
echo ""
echo "Usage:"
echo "  source .venv/bin/activate"
echo "  python3 orchestration.py                    # start cluster"
echo "  python3 orchestration.py crash-voting       # scenario 1"
echo "  python3 orchestration.py crash-after-voting  # scenario 2"
echo "  python3 orchestration.py crash-peer          # scenario 3"
echo "  python3 orchestration.py crash-submission    # scenario 4"
echo "  python3 orchestration.py crash-recovery      # scenario 5"
echo "  python3 orchestration.py fault               # fault experiment"
echo ""
echo "UI: http://localhost:9001 ~ 9004"
