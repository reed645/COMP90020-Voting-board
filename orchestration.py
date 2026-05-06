import asyncio
import os
import subprocess
import sys
import aiohttp

STATE_SERVER_URL = "http://localhost:6000"


async def start_state_server():
    process = subprocess.Popen(["python3", "state_server.py"])
    await asyncio.sleep(1)
    async with aiohttp.ClientSession() as session:
        await session.post(f"{STATE_SERVER_URL}/reset")
    return process


async def start_node(node_id, port, peers, drop_rate=0.0, sub_duration=None, vote_duration=None):
    peer_str = ",".join(str(p) for p in peers)
    env = os.environ.copy()
    env["DROP_RATE"] = str(drop_rate)
    if sub_duration is not None:
        env["SUBMISSION_DURATION"] = str(sub_duration)
    if vote_duration is not None:
        env["VOTING_DURATION"] = str(vote_duration)
    process = subprocess.Popen(
        ["python3", "node.py", "--id", str(node_id), "--port", str(port), "--peers", peer_str],
        env=env
    )
    return process


async def get_state():
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{STATE_SERVER_URL}/state") as resp:
            return await resp.json()


async def crash_node(node_id, processes):
    print(f"  >> Crashing node {node_id}...")
    processes[node_id].terminate()
    processes[node_id].wait()
    del processes[node_id]


async def recover_node(node_id, port, peers, processes, sub_duration=None, vote_duration=None):
    print(f"  >> Recovering node {node_id}...")
    processes[node_id] = await start_node(node_id, port, peers,
                                          sub_duration=sub_duration,
                                          vote_duration=vote_duration)


async def start_session_api(node_id):
    async with aiohttp.ClientSession() as session:
        await session.post(f"http://localhost:{9000 + node_id}/start")


async def submit_question_api(node_id, question):
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"http://localhost:{9000 + node_id}/submit",
            json={"question": question}
        )


async def vote_api(node_id, question_id):
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"http://localhost:{9000 + node_id}/vote",
            json={"question_ids": [question_id]}
        )


async def wait_for_phase(target_phase, timeout=120):
    for _ in range(timeout * 10):
        state = await get_state()
        if state.get("phase") == target_phase:
            return state
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for phase: {target_phase}")


async def setup_cluster(sub_duration=None, vote_duration=None):
    processes = {}
    processes["state"] = await start_state_server()
    for i in range(1, 5):
        peers = [8000 + j for j in range(1, 5) if j != i]
        processes[i] = await start_node(i, 8000 + i, peers,
                                        sub_duration=sub_duration,
                                        vote_duration=vote_duration)
        await asyncio.sleep(0.3)

    print("  Waiting for election...")
    await asyncio.sleep(6)
    state = await get_state()
    coordinator = state["coordinator_id"]
    print(f"  Coordinator: Node {coordinator}")
    return processes, coordinator


def shutdown_all(processes):
    print("  Shutting down all processes...")
    for p in processes.values():
        try:
            p.terminate()
        except Exception:
            pass
    for p in processes.values():
        try:
            p.wait(timeout=5)
        except Exception:
            pass


async def main():
    processes, coord = await setup_cluster()

    print(f"\n=== All nodes started ===")
    print(f"UI:  http://localhost:9001 ~ 9004")
    print("Press Ctrl+C to shut down all nodes.\n")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        shutdown_all(processes)


#
#  Use case 1: Coordinator crashes during voting phase.
async def crash_voting():

    processes, coord = await setup_cluster(sub_duration=30, vote_duration=10)

    await wait_for_phase("submission")
    await wait_for_phase("voting")

    # manually crash the coordinator in terminal
    input(f"\n Press Enter to crash coordinator (Node {coord})...")
    await crash_node(coord, processes)

    print(" waiting for re-election...")
    await asyncio.sleep(8)
    state = await get_state()
    new_coord = state["coordinator_id"]
    print(f" New coordinator: Node {new_coord}")
    print(f" Votes preserved: {state.get('votes', {})}")

    # recover the node after crashed for 2 seconds
    print(f"\n auto-recovering crashed node...")
    await asyncio.sleep(2)
    peers = [8000 + j for j in range(1, 5) if j != coord]
    await recover_node(coord, 8000 + coord, peers, processes,
                       sub_duration=60, vote_duration=60)

    await asyncio.sleep(2)
    state = await get_state()
    print(f"  coordinator is still: Node {state['coordinator_id']}")
    print(f"  node {coord} recovered and reclaimed coordinator role")

    input("\n  Press Enter to shut down...")
    shutdown_all(processes)


#  Use case 2: one peer node crash right after it voted.
async def crash_after_voting():
    print("\n" + "=" * 60)
    print("SCENARIO: crash-after-voting")
    print("  A peer votes then immediately crashes.")
    print("  Verifies the vote was delivered to coordinator before crash.")
    print("=" * 60)

    processes, coord = await setup_cluster(sub_duration=30, vote_duration=20)
    peer_to_crash = [i for i in range(1, 5) if i != coord][0]

    await wait_for_phase("submission")
    await wait_for_phase("voting")

    # crash node 1 after it voted.
    while True:
        state = await get_state()
        votes = state.get("votes", {})
        has_vote = any(peer_to_crash in voters for voters in votes.values())
        if has_vote:
            break
        await asyncio.sleep(0.1)
    await crash_node(peer_to_crash, processes)

    print(f"\n auto-recovering crashed node...")
    await asyncio.sleep(2)
    peers = [8000 + j for j in range(1, 5) if j != peer_to_crash]
    await recover_node(peer_to_crash, 8000 + peer_to_crash, peers, processes,
                       sub_duration=60, vote_duration=60)

    print(f"  crashed node rejoins as peer...")
    await asyncio.sleep(5)
    state = await get_state()

    input("\n  Press Enter to shut down...")
    shutdown_all(processes)


#  Use case 3: one peer node crash during submit phase.
async def crash_peer():
    print("\n" + "=" * 60)
    print("SCENARIO: crash-peer")
    print("  Non-coordinator node crashes during submission phase.")
    print("  System continues without interruption.")
    print("=" * 60)

    processes, coord = await setup_cluster(sub_duration=60, vote_duration=60)
    peer_to_crash = [i for i in range(1, 5) if i != coord][0]

    print("\n  Start the session from the UI, then submit your questions.")
    await wait_for_phase("submission")
    print("  Submission phase started! Submit questions in the UI.")

    input(f"\n  Press Enter to crash peer (Node {peer_to_crash}) during submission...")
    await crash_node(peer_to_crash, processes)

    print("  System continues normally (no re-election for peer crash).")

    print(f"\n  Auto-recovering Node {peer_to_crash} in 2 seconds...")
    await asyncio.sleep(2)
    peers = [8000 + j for j in range(1, 5) if j != peer_to_crash]
    await recover_node(peer_to_crash, 8000 + peer_to_crash, peers, processes,
                       sub_duration=60, vote_duration=60)

    print(f"  Node {peer_to_crash} sends QUERY, receives ANSWER, joins as peer...")
    await asyncio.sleep(5)
    state = await get_state()
    print(f"  Coordinator is still: Node {state['coordinator_id']}")
    print(f"  Node {peer_to_crash} rejoined as peer")

    print("  Waiting for session to complete (submission → voting → closed)...")
    await wait_for_phase("closed")
    state = await get_state()
    print(f"  Coordinator is still: Node {state['coordinator_id']} (no re-election needed)")

    input("\n  Press Enter to shut down...")
    shutdown_all(processes)


#  Use case 4: coordinator crashs during submission phase
async def crash_submission():
    print("\n" + "=" * 60)
    print("SCENARIO: crash-submission")
    print("  Coordinator crashes during submission phase.")
    print("  New coordinator recovers questions from state server.")
    print("=" * 60)

    processes, coord = await setup_cluster(sub_duration=60, vote_duration=60)

    print("\n  Start the session from the UI, then submit your questions.")
    await wait_for_phase("submission")
    print("  Submission phase started! Submit questions in the UI.")

    input(f"\n  Press Enter to crash coordinator (Node {coord}) during submission...")
    await crash_node(coord, processes)

    print("  Waiting for re-election...")
    await asyncio.sleep(8)
    state = await get_state()
    new_coord = state["coordinator_id"]
    print(f"  New coordinator: Node {new_coord}")
    print(f"  Questions recovered: {len(state.get('questions', []))}")

    print("  Session continues with remaining submission time, then voting...")

    print(f"\n  Auto-recovering Node {coord} in 2 seconds...")
    await asyncio.sleep(2)
    peers = [8000 + j for j in range(1, 5) if j != coord]
    await recover_node(coord, 8000 + coord, peers, processes,
                       sub_duration=60, vote_duration=60)

    print("  Node sends QUERY, receives ANSWER, reclaims coordinator...")
    await asyncio.sleep(5)
    state = await get_state()
    print(f"  Coordinator is still: Node {state['coordinator_id']}")
    print(f"  Node {coord} recovered and reclaimed coordinator role")

    input("\n  Press Enter to shut down...")
    shutdown_all(processes)


# Fault experiment: 50% message drop rate
async def fault_experiment():
    print("\n" + "=" * 60)
    print("FAULT EXPERIMENT")
    print("  50% drop rate on OK and COORDINATOR messages")
    print("=" * 60)

    processes = {}
    processes["state"] = await start_state_server()
    for i in range(1, 5):
        peers = [8000 + j for j in range(1, 5) if j != i]
        processes[i] = await start_node(i, 8000 + i, peers, drop_rate=0.5)
        await asyncio.sleep(0.3)

    await asyncio.sleep(8)
    state = await get_state()
    print(f"\n  Coordinator with 50% message loss: Node {state['coordinator_id']}")

    print("\n  Crashing node 4, forcing re-election with 50% message loss...")
    processes[4].terminate()
    del processes[4]
    await asyncio.sleep(10)

    state = await get_state()
    print(f"  Coordinator after re-election: Node {state['coordinator_id']}")

    print("\n  Shutting down...")
    for p in processes.values():
        p.terminate()




SCENARIOS = {
    "crash-voting": crash_voting,
    "crash-after-voting": crash_after_voting,
    "crash-peer": crash_peer,
    "crash-submission": crash_submission,
    "fault": fault_experiment,
}

if __name__ == "__main__":
    if len(sys.argv) > 1:
        name = sys.argv[1]
        if name in SCENARIOS:
            asyncio.run(SCENARIOS[name]())
        else:
            print(f"Unknown scenario: {name}")
            print(f"Available: {', '.join(SCENARIOS.keys())}")
            sys.exit(1)
    else:
        print("Usage:")
        print("  python3 orchestration.py                  # start cluster only")
        print("  python3 orchestration.py crash-voting      # coordinator crash during voting")
        print("  python3 orchestration.py crash-after-voting # coordinator crash after voting ends")
        print("  python3 orchestration.py crash-peer         # peer crash during voting")
        print("  python3 orchestration.py crash-submission   # coordinator crash during submission")
        print("  python3 orchestration.py crash-recovery     # old coordinator restarts as peer")
        print("  python3 orchestration.py fault              # 50% message drop experiment")
        print()
        asyncio.run(main())
