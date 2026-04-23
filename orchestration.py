import asyncio
import subprocess
import aiohttp

STATE_SERVER_URL = "http://localhost:6000"

async def start_state_server():
    process = subprocess.Popen(["python", "state_server.py"])
    await asyncio.sleep(1)
    async with aiohttp.ClientSession() as session:
        await session.get(f"{STATE_SERVER_URL}/reset")
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



async def main():
    process = []

    # Initialize STATE SERVER
    print("Starting state server...")
    process.append(await start_state_server())

    # Initialize 4 nodes
    print("Starting node...")
    process.append(await start_node(1,8001, [8002, 8003, 8004] ))
    process.append(await start_node(2,8002, [8001, 8003, 8004] ))
    process.append(await start_node(3,8003, [8001, 8002, 8004] ))
    process.append(await start_node(4,8004, [8001, 8002, 8003] ))

    # First election when system start
    print("Waiting for election to finish...")
    await asyncio.sleep(5)
    state = await get_state()
    print(f"coodinator: {state['coordinator_id']}")

    # Shut down all processes
    print("Shutting down...")
    for p in process:
        p.terminate()

if __name__ == "__main__":
    asyncio.run(main())


