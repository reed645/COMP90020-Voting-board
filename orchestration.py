import asyncio
import subprocess
import aiohttp

STATE_SERVER_URL = "http://localhost:6000"

async def start_state_server():
    process = subprocess.Popen(["python", "state_server.py"])
    await asyncio.sleep(1)
    async with aiohttp.ClientSession() as session:
        await session.post(f"{STATE_SERVER_URL}/reset")
    return process

async def start_node(node_id, port, peers):
    peer_str = ",".join(str(p) for p in peers)
    process = subprocess.Popen(
        ["python", "node.py", "--id" ,str(node_id), "--port",  str(port), "--peers",  str(peer_str)])
    return process

async def get_state():
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{STATE_SERVER_URL}/state") as resp:
            return await resp.json()

async def crush_node(node_id, process):
    print("Crushing node...")
    process[node_id].terminate()
    del process[node_id]

async def recover_node(node_id, port, peers, process):
    print(f"Recovering node {node_id}...")
    process[node_id] = await start_node(node_id, port, peers)
    await asyncio.sleep(1)

async def main():
    process = {}
    process["state"] = await start_state_server()
    process[1] = await start_node(1, 8001, [8002, 8003, 8004])
    await asyncio.sleep(0.3)
    process[2] = await start_node(2, 8002, [8001, 8003, 8004])
    await asyncio.sleep(0.3)
    process[3] = await start_node(3, 8003, [8001, 8002, 8004])
    await asyncio.sleep(0.3)
    process[4] = await start_node(4, 8004, [8001, 8002, 8003])

    # First election when system start
    print("Waiting for election to finish...")
    await asyncio.sleep(5)
    state = await get_state()
    print(f"coodinator: {state['coordinator_id']}")

    # Crush the highest id node
    await crush_node(4, process)
    print("Waiting for re-election...")
    await asyncio.sleep(8)
    state = await get_state()
    print(f"New coordinator after crush: {state['coordinator_id']}")

    # Recover the highest id node
    await recover_node(4, 8004, [8001, 8002, 8003], process)
    await asyncio.sleep(4)
    state = await get_state()
    print(f"New coordinator after recover: {state['coordinator_id']}")

    # Shut down all processes
    print("Shutting down...")
    for p in process.values():
        p.terminate()

if __name__ == "__main__":
    asyncio.run(main())


