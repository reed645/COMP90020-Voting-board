import asyncio
import subprocess
import aiohttp

STATE_SERVER_URL = "http://localhost:6000"

async def start_state_server():
    process = subprocess.Popen(["python", "state_server.py"])
    await asyncio.sleep(1)
    return process

async def start_node(node_id, port, peers):
    peer_str = ",".join(str(p) for p in peers)
    process = subprocess.Popen(
        ["python", "node.py", "--id" ,str(node_id), "--port",  str(port), "--peers",  str(peer_str)])
    return process