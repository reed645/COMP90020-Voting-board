# Distributed Voting System

## Quick Start

```bash
# 1. Setup enviornment
chmod +x setup.sh
./setup.sh

# 2. Activate environment
source .venv/bin/activate

# 3. Start the system
python3 orchestration.py
```

Open browser tabs at `http://localhost:9001` ~ `http://localhost:9004` to access each node's UI.

## Dependencies

- Python 3.9+
- `websockets` — inter-node communication
- `aiohttp` — web UI server + HTTP client
- `flask` — state server

## Demo Scenarios

```bash
# Start system to see first election
python3 orchestration.py

# Scenario 1: crash coodinator during voting phase
python3 orchestration.py crash-voting

# Scenario 2: crash one peer after it voted
python3 orchestration.py crash-after-voting

# Scenario 3: crash one peer during submission phase
python3 orchestration.py crash-peer

# Scenario 4: crash coodinator during submission phase
python3 orchestration.py crash-submission
```

