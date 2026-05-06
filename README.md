# Distributed Voting System

## Quick Start

```bash
# 1. Setup (creates venv + installs dependencies)
chmod +x setup.sh
./setup.sh

# 2. Activate virtual environment
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
# Start cluster only (no crash simulation)
python3 orchestration.py

# Scenario 1: Coordinator crashes during voting phase
python3 orchestration.py crash-voting

# Scenario 2: Peer votes then immediately crashes (verify vote delivery)
python3 orchestration.py crash-after-voting

# Scenario 3: Peer crashes during submission phase (system continues)
python3 orchestration.py crash-peer

# Scenario 4: Coordinator crashes during submission phase
python3 orchestration.py crash-submission

# Fault experiment: 50% message drop rate
python3 orchestration.py fault
```

