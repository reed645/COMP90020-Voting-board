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


async def start_node(node_id, port, peers, drop_rate=0.0, sub_duration=None, vote_duration=None,
                     crash_after_vote=False):
    peer_str = ",".join(str(p) for p in peers)
    env = os.environ.copy()
    env["DROP_RATE"] = str(drop_rate)
    if sub_duration is not None:
        env["SUBMISSION_DURATION"] = str(sub_duration)
    if vote_duration is not None:
        env["VOTING_DURATION"] = str(vote_duration)
    if crash_after_vote:
        env["CRASH_AFTER_VOTE"] = "1"
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

    processes, coord = await setup_cluster(sub_duration=30, vote_duration=20)

    await wait_for_phase("submission")
    await wait_for_phase("voting")

    # manually crash the coordinator in terminal
    input(f"\n press Enter to crash coordinator...")
    await crash_node(coord, processes)

    print(" waiting for re-election...")
    await asyncio.sleep(8)
    state = await get_state()
    new_coord = state["coordinator_id"]
    print(f" New coordinator: Node {new_coord}")

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

    input("\n  press Enter to shut down...")
    shutdown_all(processes)


#  Use case 2: one peer node crash right after it voted.
async def crash_after_voting():
    processes = {}
    processes["state"] = await start_state_server()

    # only crash node 1 after it voted.
    coord = 4
    peer_to_crash = 1
    for i in range(1, 5):
        peers = [8000 + j for j in range(1, 5) if j != i]
        processes[i] = await start_node(
            i, 8000 + i, peers,
            sub_duration=30, vote_duration=20,
            crash_after_vote=(i == peer_to_crash)
        )
        await asyncio.sleep(0.3)

    print("  waiting for election...")
    await asyncio.sleep(6)
    state = await get_state()
    coord = state["coordinator_id"]
    print(f"  Coordinator: Node {coord}")

    await wait_for_phase("submission")
    await wait_for_phase("voting")

    # wait for the process to exit
    processes[peer_to_crash].wait()
    print(f"  node 1 crashed after voting.")
    del processes[peer_to_crash]

    print(f"\n  auto-recovering crashed node in 2 seconds...")
    await asyncio.sleep(2)
    peers = [8000 + j for j in range(1, 5) if j != peer_to_crash]
    await recover_node(peer_to_crash, 8000 + peer_to_crash, peers, processes,
                       sub_duration=30, vote_duration=20)

    print(f"  crashed node rejoins as peer...")
    await asyncio.sleep(5)
    state = await get_state()

    input("\n  press Enter to shut down...")
    shutdown_all(processes)


#  Use case 3: one peer node crash during submit phase.
async def crash_peer():

    processes, coord = await setup_cluster(sub_duration=30, vote_duration=20)
    peer_to_crash = [i for i in range(1, 5) if i != coord][0]

    await wait_for_phase("submission")

    input(f"\n  press Enter to crash peer (Node {peer_to_crash}) during submission...")
    await crash_node(peer_to_crash, processes)

    print(f"\n  auto-recovering crashed node in 2 seconds...")
    await asyncio.sleep(2)
    peers = [8000 + j for j in range(1, 5) if j != peer_to_crash]
    await recover_node(peer_to_crash, 8000 + peer_to_crash, peers, processes,
                       sub_duration=60, vote_duration=60)

    print(f"  node sends QUERY, receives ANSWER, joins as peer...")
    await asyncio.sleep(5)
    state = await get_state()
    print(f"  Node {peer_to_crash} rejoined as peer")

    await wait_for_phase("closed")
    state = await get_state()

    input("\n  Press Enter to shut down...")
    shutdown_all(processes)


#  Use case 4: coordinator crashs during submission phase
async def crash_submission():
    processes, coord = await setup_cluster(sub_duration=30, vote_duration=20)

    await wait_for_phase("submission")

    input(f"\n  press Enter to crash coordinator...")
    await crash_node(coord, processes)

    print("  waiting for re-election...")
    await asyncio.sleep(8)
    state = await get_state()
    new_coord = state["coordinator_id"]
    print(f"  New coordinator: Node {new_coord}")

    print(f"\n  auto-recovering crashed in 2 seconds...")
    await asyncio.sleep(2)
    peers = [8000 + j for j in range(1, 5) if j != coord]
    await recover_node(coord, 8000 + coord, peers, processes,
                       sub_duration=60, vote_duration=60)

    print("  node sends QUERY, receives ANSWER, reclaims coordinator...")
    await asyncio.sleep(5)
    state = await get_state()
    print(f"  coordinator is still: Node {state['coordinator_id']}")

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
        print("  python3 orchestration.py fault              # 50% message drop experiment")
        print()
        asyncio.run(main())
