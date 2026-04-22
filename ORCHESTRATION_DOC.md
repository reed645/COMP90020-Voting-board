# Voting Application - System Documentation

## Architecture Overview

The system consists of **4 components**:

```
┌─────────────────────────────────────────────────────────────────┐
│                      Orchestration Layer                        │
│                                                                 │
│                                                                 │
│  - Start/kill/recover multiple Node processes                   │
│  - Collect and visualize traces                                 │
│  - Simulate crashes and network partitions                      │
└─────────────────────────────────────────────────────────────────┘
           │                    │                    │
           ▼                    ▼                    ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│  Node P3      │    │  Node P5      │    │  Node P7      │
│  Port 8003    │◄──►│  Port 8005    │◄──►│  Port 8007    │
│               │    │  (coordinator)│    │               │
└───────────────┘    └───────────────┘    └───────────────┘
           │                                       │
           └───────────────┬───────────────────────┘
                           ▼
                 ┌─────────────────────┐
                 │   State Server      │
                 │   HTTP :6000        │
                 │   state.json        │
                 └─────────────────────┘
```

**Important convention:** `port == node_id`. Node with ID=3 uses port 8003.

---

## File Inventory

### 1. `node.py` — The Node 

The main distributed node. Each instance is a single Python process.

**Entry point:**
```python
# Start a node manually:
python node.py --id 3 --port 8003 --peers 8005,8007
```

**Key public methods for orchestration:**

| Method | Description |
|--------|-------------|
| `node.start()` | Start the node (connect to peers, start monitors) |
| `node.stop()` | Graceful shutdown |
| `node.crash()` | Simulate crash (force close all connections) |
| `node.recover()` | Recover from crash and rejoin cluster |
| `node.submit_question(text)` | Submit a question (peer → coordinator) |
| `node.vote(question_ids)` | Vote for questions (peer → coordinator) |

**Key internal methods for understanding:**

| Method | Lines | Description |
|--------|-------|-------------|
| `_start_election()` | 447-487 | Initiate Modified Bully election |
| `_handle_ok()` | 539-553 | Respond to OK from higher-ID node |
| `_handle_coordinator()` | 665-699 | Handle COORDINATOR broadcast |
| `_query_for_coordinator()` | 555-577 | Find coordinator via QUERY |
| `_heartbeat_monitor()` | 420-445 | Monitor coordinator heartbeat |
| `_election_monitor()` | 777-789 | Monitor for lost coordinator |
| `_run_submission_phase()` | 794-818 | Run submission phase (60s default) |
| `_run_voting_phase()` | 862-883 | Run voting phase (30s default) |
| `_resume_session()` | 701-745 | Resume session after election |
| `_sync_with_state_server()` | 222-240 | Read state from state server |

---

### 2. `messages.py` — Message Types (line count: 235)

Defines all inter-node message formats.

**Message structure:**
```python
@dataclass
class Message:
    type: str           # e.g., "ELECTION", "HEARTBEAT", "SUBMIT"
    sender_id: int      # node ID of sender
    payload: dict       # type-specific data
```

**All message types:**

| Type | Direction | Payload |
|------|-----------|---------|
| `ELECTION` | Any → Higher IDs | `{}` |
| `OK` | Higher → Lower | `{}` |
| `QUERY` | Any → All | `{}` |
| `ANSWER` | Any → Querier | `{"coordinator_id": int}` |
| `COORDINATOR` | Announcer → All | `{"coordinator_id": int}` |
| `HEARTBEAT` | Coordinator → All | `{"coordinator_id": int, "phase": str}` |
| `SUBMIT` | Peer → Coordinator | `{"question": str}` |
| `VOTE` | Peer → Coordinator | `{"question_ids": list}` |
| `QUESTIONS` | Coordinator → All | `{"questions": list}` |
| `RESULT` | Coordinator → All | `{"rankings": list}` |
| `SESSION_UPDATE` | Coordinator → All | `{"phase": str}` |
| `SESSION_RESUME` | New Coordinator → All | `{"phase": str, "questions": list, "votes": dict}` |

**Create a message:**
```python
from messages import create_message, Message
msg = create_message("SUBMIT", sender_id=3, payload={"question": "What color?"})
```

---

### 3. `state_server.py` — State Server (line count: 208)

Flask HTTP server on port 6000. Persists session state to `state.json`.

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/state` | GET | Get full state |
| `/state` | POST | Replace full state |
| `/state/questions` | POST | Add question (dedup by content) |
| `/state/votes` | POST | Add vote (dedup by sender) |
| `/state/phase` | POST | Update phase |
| `/reset` | POST | Reset to default state |
| `/health` | GET | Health check |

**State schema:**
```json
{
    "coordinator_id": null,
    "phase": "submission",
    "questions": [{"id": "q1", "text": "...", "submitted_by": 3}],
    "votes": {"q1": [3, 5, 7]},
    "submission_deadline": 1234567890.123,
    "voting_deadline": 1234567890.123
}
```

**Run the state server:**
```bash
python state_server.py
# Runs on http://localhost:6000
```

---

### 4. `config.py` — Configuration (line count: 23)

All timing and constant configuration.

```python
# Heartbeat settings
HEARTBEAT_INTERVAL = 1.0    # seconds between heartbeats
TIMEOUT = 3.0               # seconds without heartbeat before declaring dead
ELECTION_TIMEOUT = 2.0      # wait time for OK replies
QUERY_TIMEOUT = 2.0         # wait time for ANSWER replies

# State server
STATE_SERVER_URL = "http://localhost:6000"

# Session durations
SUBMISSION_DURATION = 60    # seconds
VOTING_DURATION = 30        # seconds

# Phases
PHASE_SUBMISSION = "submission"
PHASE_VOTING = "voting"
PHASE_CLOSED = "closed"
```

---

## Node Lifecycle

### Startup Flow (`start()`)

```
start()
  ├─ _sync_with_state_server()     ← Read coordinator_id, phase from state server
  ├─ start WebSocket server        ← Listen for peer connections
  ├─ _connect_to_peers()           ← Connect to all known peers
  ├─ _heartbeat_monitor()          ← Background: check heartbeat timeout
  ├─ _election_monitor()           ← Background: check for lost coordinator
  └─ coordinator_id is None?
       └─ _start_election()        ← Become coordinator if no one else
```

### Recovery Flow (`recover()`)

```
recover()
  ├─ _sync_with_state_server()
  ├─ restart WebSocket server
  ├─ _connect_to_peers()
  ├─ _heartbeat_monitor()
  └─ _query_for_coordinator()       ← Ask peers who the coordinator is
       ├─ Get ANSWER (higher ID) → accept as peer
       ├─ Get ANSWER (lower ID) → reject, start election
       └─ Timeout → start election
```

### Crash Flow (`crash()`)

```
crash()
  ├─ running = False
  ├─ force close all connections
  └─ close WebSocket server
```

**Note:** `crash()` does NOT cancel tasks or stop monitors. The orchestrator should handle process termination.

---

## Election Algorithm (Modified Bully)

### When is election triggered?

| Trigger | Location |
|---------|----------|
| Node starts, no coordinator exists | `start()` line 145 |
| Heartbeat timeout (no heartbeat from coordinator) | `_heartbeat_monitor()` line 445 |
| Receive ANSWER with lower coordinator_id | `_handle_answer()` line 625 |
| QUERY timeout (no ANSWER received) | `_query_for_coordinator()` line 577 |
| Periodic check (no coordinator, not in election) | `_election_monitor()` line 789 |

### Election flow

```
Node P3 starts election:
  1. Send ELECTION to all higher-ID nodes (P5, P7)
  2. Wait for OK replies OR timeout (ELECTION_TIMEOUT = 2s)
     ├─ All OKs received → cancel timer → determine winner
     └─ Timeout → determine winner
  3. Determine winner:
     ├─ Got OKs → winner = max(ok_received)
     └─ No OKs → winner = self
  4. Broadcast COORDINATOR(winner_id)
  5. If winner == self → become coordinator, start session
```

### Key invariant

Higher-ID nodes always win. If P3 and P5 both start elections simultaneously:
- P5 receives P3's ELECTION, sends OK, and ignores the rest
- P3 receives P5's ELECTION, sends OK, and ignores P5's lower-ID ELECTION
- P5 wins (higher ID) and broadcasts COORDINATOR(P5)
- P3 becomes peer of P5

---

## Session Lifecycle

### Phases

```
SUBMISSION (60s) → VOTING (30s) → CLOSED
```

### Coordinator responsibilities by phase

| Phase | Coordinator Actions |
|-------|-------------------|
| SUBMISSION | Accept SUBMIT messages, deduplicate, store in state server |
| VOTING | Accept VOTE messages, deduplicate by sender, store in state server |
| CLOSED | Broadcast final RESULTS, then wait |

### Peer responsibilities by phase

| Phase | Peer Actions |
|-------|-------------|
| SUBMISSION | Can submit one question via `node.submit_question()` |
| VOTING | Can vote for questions via `node.vote([qid1, qid2])` |
| CLOSED | Receive final results |

### Phase transition on coordinator crash

When coordinator crashes during a session:
1. Peers detect heartbeat timeout → start election
2. New coordinator elected → `_resume_session()`
3. `_resume_session()` reads state from state server
4. Calculates remaining time and continues from where left off
5. Broadcasts SESSION_RESUME with current state

---

## Trace Events

All node actions emit JSON traces to stdout via `emit_trace()`:

```python
emit_trace(event, node_id, detail)
```

**Example trace:**
```json
{"time": 1234567890.123, "node_id": 5, "event": "ELECTION_SENT", "detail": {"term": 1}}
```

### Complete event list

| Event | When |
|-------|------|
| `NODE_STARTED` | Node started |
| `NODE_STOPPED` | Node stopped gracefully |
| `CRASH` | Node crashed |
| `RECOVERY_STARTED` | Node recovered |
| `STATE_SYNCED` | Synced with state server |
| `STATE_WRITE_ERROR` | Failed to write to state server |
| `PEER_CONNECTED` | Connected to peer |
| `PEER_DISCONNECTED` | Peer disconnected |
| `PEER_CONNECT_FAILED` | Failed to connect to peer |
| `MESSAGE_RECEIVED` | Any message received |
| `INVALID_MESSAGE` | Malformed message |
| `MESSAGE_HANDLER_ERROR` | Handler threw exception |
| `CLIENT_HANDLER_ERROR` | Client handler threw exception |
| `HEARTBEAT_TIMEOUT` | No heartbeat from coordinator |
| `ELECTION_SENT` | Started election |
| `ELECTION_RECEIVED` | Received ELECTION from peer |
| `OK_RECEIVED` | Received OK reply |
| `QUERY_SENT` | Sent QUERY to find coordinator |
| `COORDINATOR_LOST` | QUERY timed out, no coordinator found |
| `ANSWER_SENT` | Sent ANSWER to QUERY |
| `ANSWER_RECEIVED` | Received ANSWER |
| `ELECTION_COMPLETE` | Election winner determined |
| `COORDINATOR_ELECTED` | Broadcasting COORDINATOR |
| `COORDINATOR_BROADCAST` | Received COORDINATOR broadcast |
| `COORDINATOR_ACCEPTED` | Accepted new coordinator |
| `UNFROZEN` | Unfrozen (election ended) |
| `SUBMISSION_STARTED` | Submission phase began |
| `SUBMISSION_ENDED` | Submission phase ended |
| `PHASE_ADVANCED` | Phase changed |
| `VOTING_STARTED` | Voting phase began |
| `RESULT_BROADCAST` | Live results broadcast |
| `SESSION_CLOSED` | Session closed |
| `FINAL_RESULT_BROADCAST` | Final results broadcast |
| `SUBMIT_RECEIVED` | Received question submission |
| `SUBMIT_REJECTED` | Submission rejected |
| `SUBMIT_SENT` | Submitted question |
| `VOTE_RECEIVED` | Received vote |
| `VOTE_REJECTED` | Vote rejected |
| `VOTE_SENT` | Voted |
| `QUESTIONS_RECEIVED` | Received questions list |
| `RESULT_RECEIVED` | Received results |
| `SESSION_RESUMED` | Session resumed on new coordinator |
| `SESSION_UPDATE` | Phase change notification |
| `LATE_JOIN_CLOSED` | Joined after session closed |
| `LATE_JOIN_VOTING` | Joined during voting phase |
| `LATE_JOIN_MISSED_SUBMISSION` | Joined after submission ended |

---

## Orchestration API (What you need to build)

### Node instantiation

```python
from node import Node

node = Node(
    node_id=3,           # Must match port convention
    port=8003,           # port == node_id * 1000 + 3000, so 3 → 8003
    peer_ports=[8005, 8007]
)
```

### Running a node (needs asyncio)

```python
import asyncio

async def main():
    node = Node(node_id=3, port=8003, peer_ports=[8005, 8007])
    await node.start()
    # Node runs until crash or stop()

asyncio.run(main())
```

### Simulating scenarios

**Scenario 1: Start all nodes**
```python
import asyncio

async def start_cluster():
    nodes = {}
    for node_id in [3, 5, 7]:
        port = node_id * 1000 + 3000
        peer_ports = [n * 1000 + 3000 for n in [3, 5, 7] if n != node_id]
        node = Node(node_id=node_id, port=port, peer_ports=peer_ports)
        nodes[node_id] = node
        asyncio.create_task(node.start())
    return nodes

asyncio.run(start_cluster())
```

**Scenario 2: Kill coordinator**
```python
# Wait for election to complete
await asyncio.sleep(5)

# Find coordinator
for node_id, node in nodes.items():
    if node.role == "coordinator":
        await node.crash()
        print(f"Crashed coordinator: {node_id}")
        break
```

**Scenario 3: Recover node**
```python
# Recover crashed node
await nodes[crashed_id].recover()
```

**Scenario 4: Submit and vote**
```python
# Wait for coordinator to be elected
await asyncio.sleep(3)

# Submit question (only during submission phase)
for node_id, node in nodes.items():
    if node.role != "coordinator":
        await node.submit_question("What is your favorite color?")

# Wait for voting phase
await asyncio.sleep(65)

# Vote (only during voting phase)
for node_id, node in nodes.items():
    if node.role != "coordinator":
        await node.vote(["q1"])
```

---

## Key Implementation Details for Orchestration

### 1. Node ID ↔ Port convention

```python
port = node_id * 1000 + 3000

# Examples:
# node_id=3  → port=8003
# node_id=5  → port=8005
# node_id=7  → port=8007
```

### 2. Running state server first

The state server MUST be running before any nodes start:

```bash
# Terminal 1
python state_server.py
```

### 3. Resetting state between tests

```python
import requests
requests.post("http://localhost:6000/reset")
```

### 4. Capturing traces

Traces are printed to stdout. Parse the JSON for visualization:

```python
import json
import subprocess
import threading

def trace_listener(process):
    for line in process.stdout:
        try:
            trace = json.loads(line)
            # Handle trace event
            print(f"Node {trace['node_id']}: {trace['event']}")
        except json.JSONDecodeError:
            pass

# Start node process
proc = subprocess.Popen(["python", "node.py", "--id", "3", ...])
thread = threading.Thread(target=trace_listener, args=(proc,))
thread.start()
```

### 5. Timing considerations

- Heartbeat interval: 1 second
- Heartbeat timeout: 3 seconds
- Election timeout: 2 seconds
- QUERY timeout: 2 seconds
- Submission phase: 60 seconds
- Voting phase: 30 seconds

When simulating scenarios:
- Wait at least 3 seconds after killing coordinator before checking new coordinator
- Wait at least 5 seconds for election to complete
- Wait at least 65 seconds for submission → voting transition

### 6. Frozen state

During an election, if a node is acting as coordinator:
- It sets `frozen = True`
- Stops accepting SUBMIT and VOTE messages
- Unfreezes when COORDINATOR broadcast received

### 7. Race conditions

The system has these known race conditions (acceptable for simulation):
- Multiple nodes can start elections simultaneously
- Higher-ID node always wins
- Election can trigger during session phase transition
- Late-joining node may miss parts of session

---
