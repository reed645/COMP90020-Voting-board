""" 
    This is the node definition.
    Each node is a single process that can be run on the same machine.
    Nodes communicate via WebSockets and elect a coordinator using the Modified Bully algorithm.
"""

import argparse
import asyncio
import json
import time
import uuid
from typing import Dict, Optional, Set
import websockets
from websockets.legacy.server import serve
from websockets.legacy.client import connect as ws_connect
import aiohttp
import os
import random

from aiohttp import web

from config import (
    HEARTBEAT_INTERVAL,
    TIMEOUT,
    ELECTION_TIMEOUT,
    QUERY_TIMEOUT,
    STATE_SERVER_URL,
    SUBMISSION_DURATION,
    VOTING_DURATION,
    PHASE_WAITING,
    PHASE_SUBMISSION,
    PHASE_VOTING,
    PHASE_CLOSED,
)
from messages import Message, create_message


_node_instances: Dict[int, "Node"] = {}


def emit_trace(event: str, node_id: int, detail: dict = None):
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    detail_str = "  " + ", ".join(f"{k}={v}" for k, v in (detail or {}).items())
    print(f"[{ts}] Node {node_id} | {event:<35} |{detail_str}")
    node = _node_instances.get(node_id)
    if node:
        node._push_log(ts, event, detail)


class Node:

    def __init__(self, node_id: int, port: int, peer_ports: list):
        
        # NOTE: our implementation assumes port number encodes the node ID,
        # e.g., node_id=3 uses port=8003, node_id=5 uses port=8005.
        # The peer_ports contains list of port numbers
        #  (port == node_id convention)

        self.node_id = node_id
        self.port = port
        
        self.peer_ports = peer_ports

        # Role state
        self.role = "peer" 
        self.coordinator_id: Optional[int] = None
        #the term number for the election
        self.current_term = 0

        self.known_nodes: Dict[int, dict] = {} 

        # Election state
        self.election_state = {
            "in_progress": False,
            "ok_received": [], 
            "timer": None,
            "higher_nodes_contacted": [],  
        }

        # Timing
        self.last_heartbeat: float = time.time()
        self.heartbeat_task: Optional[asyncio.Task] = None


        # Session state
        self.session_phase = PHASE_WAITING
        #whether submitted the question
        self.has_submitted = False
        #whether voted for the question
        self.has_voted = False
        self.questions: list = []
        #the votes for the question
        #question_id -> [sender_ids]
        self.votes: dict = {}  
        self.submission_deadline: Optional[float] = None
        self.voting_deadline: Optional[float] = None
        self.latest_rankings: list = []

        # Coordinator timers (when the node becomes coordinator)
        self.submission_timer_task: Optional[asyncio.Task] = None
        self.voting_timer_task: Optional[asyncio.Task] = None
        self.vote_broadcast_task: Optional[asyncio.Task] = None

        # frozen state (when another election is in progress while we are coordinator)
        #stop receiving vote and submit
        #defreeze when the election is over
        self.frozen = False
        self.frozen_timer: Optional[asyncio.Task] = None

    
        self.websocket_server: Optional[websockets.WebSocketServer] = None
        self.outgoing_connections: Dict[int, websockets.WebSocketClientProtocol] = {}  # port -> connection

        
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.running = False

        # unique identifier for this node instance (in case the node crash)
        self.instance_id = str(uuid.uuid4())[:8]

        # wait for old coordinator to step down before taking over
        self._step_down_ack_event = asyncio.Event()

        # Web UI state
        self._ws_clients: Set = set()
        self._peer_connect_failures: Set = set()
        self._session_task: asyncio.Task = None


    async def start(self):
        self.loop = asyncio.get_event_loop()
        self.running = True
        _node_instances[self.node_id] = self

        # connect to state server and get initial state
        await self._sync_with_state_server()

        self.websocket_server = await serve(
            self._handle_client,
            host="localhost",
            port=self.port
        )

        # start web UI server
        await self._start_web_server()

        emit_trace("NODE_STARTED", self.node_id, {
            "port": self.port,
            "peer_ports": self.peer_ports,
            "coordinator_id": self.coordinator_id,
            "phase": self.session_phase
        })

        # connect to known peers
        await self._connect_to_peers()

        # start heartbeat for coordinator
        self.heartbeat_task = asyncio.create_task(self._heartbeat_monitor())

        # Start election monitor
        asyncio.create_task(self._election_monitor())

        #  query for coordinator on startup, triggers election if no coordinator exists
        asyncio.create_task(self._query_for_coordinator())

    async def stop(self):
        #stop the node gracefully
        self.running = False

        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.submission_timer_task:
            self.submission_timer_task.cancel()
        if self.voting_timer_task:
            self.voting_timer_task.cancel()
        if self.vote_broadcast_task:
            self.vote_broadcast_task.cancel()
        if self.frozen_timer:
            self.frozen_timer.cancel()

        if self.election_state["timer"]:
            self.election_state["timer"].cancel()

        # close outgoing connections
        for conn in self.outgoing_connections.values():
            try:
                await conn.close()
            except Exception:
                pass

        # close server
        if self.websocket_server:
            self.websocket_server.close()
            await self.websocket_server.wait_closed()

        emit_trace("NODE_STOPPED", self.node_id)

    async def crash(self):
        #simulate node crash
        emit_trace("CRASH", self.node_id)
        self.running = False
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.submission_timer_task:
            self.submission_timer_task.cancel()
        if self.voting_timer_task:
            self.voting_timer_task.cancel()
        if self.vote_broadcast_task:
            self.vote_broadcast_task.cancel()
        if self.frozen_timer:
            self.frozen_timer.cancel()
        if self.election_state["timer"]:
            self.election_state["timer"].cancel()

        # force close all connections
        for conn in self.outgoing_connections.values():
            try:
                conn.close(1001, "Node crashed")
            except Exception:
                pass
        self.outgoing_connections.clear()

        if self.websocket_server:
            self.websocket_server.close()
            await self.websocket_server.wait_closed()

    async def recover(self):
        #simulate recover from crash and rejoin the cluster
        emit_trace("RECOVERY_STARTED", self.node_id)
        self.running = True

        # re-sync with state server
        await self._sync_with_state_server()

        # check if recovering into an ongoing session
        await self.check_and_handle_late_join()

        # restart WebSocket server
        self.websocket_server = await serve(
            self._handle_client,
            host="localhost",
            port=self.port
        )

        # reconnect to peers
        await self._connect_to_peers()

        # Start monitoring again
        self.last_heartbeat = time.time()
        self.heartbeat_task = asyncio.create_task(self._heartbeat_monitor())

        # find coordinator via QUERY
        await self._query_for_coordinator()

    #the operations on the state server: sync, write, add question, add vote, update phase
    async def _sync_with_state_server(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{STATE_SERVER_URL}/state") as resp:
                    if resp.status == 200:
                        state = await resp.json()
                        self.coordinator_id = state.get("coordinator_id")
                        self.session_phase = state.get("phase", PHASE_WAITING)
                        self.questions = state.get("questions", [])
                        self.votes = state.get("votes", {})
                        self.submission_deadline = state.get("submission_deadline")
                        self.voting_deadline = state.get("voting_deadline")

                        if any(q.get("submitted_by") == self.node_id for q in self.questions):
                            self.has_submitted = True

                        emit_trace("STATE_SYNCED", self.node_id, {
                            "phase": self.session_phase,
                            "coordinator_id": self.coordinator_id
                        })
        except Exception as e:
            emit_trace("STATE_SYNC_ERROR", self.node_id, {"error": str(e)})



    async def _add_question_to_server(self, question: str, submitted_by: int) -> Optional[dict]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{STATE_SERVER_URL}/state/questions",
                    json={"question": question, "submitted_by": submitted_by}
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result
        except Exception as e:
            emit_trace("QUESTION_ADD_ERROR", self.node_id, {"error": str(e)})
        return None

    async def _add_vote_to_server(self, question_id: str, sender_id: int) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{STATE_SERVER_URL}/state/votes",
                    json={"question_id": question_id, "sender_id": sender_id}
                ) as resp:
                    if resp.status == 200:
                        return True
        except Exception as e:
            emit_trace("VOTE_ADD_ERROR", self.node_id, {"error": str(e)})
        return False

    async def _update_phase_to_server(self, phase: str = None, submission_deadline: float =
    None, voting_deadline: float = None):
        try:
            async with aiohttp.ClientSession() as session:
                payload = {}
                if phase is not None:
                    payload["phase"] = phase
                if submission_deadline is not None:
                    payload["submission_deadline"] = submission_deadline
                if voting_deadline is not None:
                    payload["voting_deadline"] = voting_deadline
                async with session.post(
                        f"{STATE_SERVER_URL}/state/phase",
                        json=payload
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            emit_trace("PHASE_UPDATE_ERROR", self.node_id, {"error": str(e)})
        return False

    async def _update_coordinator_to_server(self, coordinator_id: int):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{STATE_SERVER_URL}/state/coordinator",
                        json={"coordinator_id": coordinator_id}
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            emit_trace("COORDINATOR_UPDATE_ERROR", self.node_id, {"error": str(e)})
        return False

    async def _read_from_outgoing(self, conn, port):
        try:
            async for msg_json in conn:
                try:
                    message = Message.from_json(msg_json)
                    await self._handle_message(message, conn)
                except Exception:
                    pass

        except websockets.ConnectionClosedOK:
            pass
        finally:
            if port in self.outgoing_connections:
                del self.outgoing_connections[port]

    async def _connect_to_peers(self):
        # establish outgoing WebSocket connections to all known peers
        for port in self.peer_ports:
            #check if the port is not the same as the current node and not already connected
            if port != self.port and port not in self.outgoing_connections:
                try:
                    conn = await asyncio.wait_for(
                        ws_connect(f"ws://localhost:{port}"),
                        timeout=2.0
                    )
                    self.outgoing_connections[port] = conn
                    self._peer_connect_failures.discard(port)
                    asyncio.create_task(self._read_from_outgoing(conn, port))
                    emit_trace("PEER_CONNECTED", self.node_id, {"port": port})
                except Exception as e:
                    if port not in self._peer_connect_failures:
                        self._peer_connect_failures.add(port)
                        emit_trace("PEER_CONNECT_FAILED", self.node_id, {
                            "port": port,
                            "error": str(e)
                        })

    async def _broadcast(self, message: Message, exclude_port: int = None):
        await self._broadcast_to_ports(message, list(self.outgoing_connections.keys()), exclude_port)

    async def _broadcast_to_ports(self, message: Message, ports: list, exclude_port: int = None):
        msg_json = message.to_json()
        tasks = []

        for port in ports:
            if port != exclude_port and port in self.outgoing_connections:
                try:
                    tasks.append(self.outgoing_connections[port].send(msg_json))
                except Exception:
                    del self.outgoing_connections[port]

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_client(self, websocket: websockets.WebSocketClientProtocol, path: str):
        #handle WebSocket connections from peers
        #to see if new peer connects
        client_port = websocket.remote_address[1] if websocket.remote_address else None

        try:
            async for msg_json in websocket:
                try:
                    message = Message.from_json(msg_json)
                    await self._handle_message(message, websocket)
                except json.JSONDecodeError:
                    emit_trace("INVALID_MESSAGE", self.node_id, {"raw": msg_json[:100]})
                except Exception as e:
                    emit_trace("MESSAGE_HANDLER_ERROR", self.node_id, {
                        "error": str(e),
                        "message": msg_json[:100]
                    })
            #ignore if the peer disconnects normally
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            emit_trace("CLIENT_HANDLER_ERROR", self.node_id, {"error": str(e)})
        finally:
            # remove from known nodes if connection closed
            if client_port and client_port in self.outgoing_connections:
                del self.outgoing_connections[client_port]
                emit_trace("PEER_DISCONNECTED", self.node_id, {"port": client_port})

    async def _handle_message(self, message: Message, connection: websockets.WebSocketClientProtocol = None):
        #route incoming messages to appropriate handlers
        msg_type = message.type
        sender_id = message.sender_id
        payload = message.payload

        if msg_type != "HEARTBEAT":
            emit_trace("MESSAGE_RECEIVED", self.node_id, {
                "type": msg_type,
                "sender_id": sender_id
            })

        #any message from coordinator updates the last heartbeat
        if sender_id == self.coordinator_id:
            self.last_heartbeat = time.time()

        if msg_type == "HEARTBEAT":
            await self._handle_heartbeat(payload)
        elif msg_type == "ELECTION":
            await self._handle_election(message, connection)
        elif msg_type == "OK":
            await self._handle_ok(sender_id)
        elif msg_type == "QUERY":
            await self._handle_query(message, connection)
        elif msg_type == "ANSWER":
            await self._handle_answer(payload)
        elif msg_type == "COORDINATOR":
            await self._handle_coordinator(message, connection)
        elif msg_type == "SUBMIT":
            await self._handle_submit(message)
        elif msg_type == "VOTE":
            await self._handle_vote(message)
        elif msg_type == "QUESTIONS":
            await self._handle_questions(payload)
        elif msg_type == "RESULT":
            await self._handle_result(payload)
        elif msg_type == "SESSION_UPDATE":
            await self._handle_session_update(payload)
        elif msg_type == "SESSION_RESUME":
            await self._handle_session_resume(payload)
        elif msg_type == "START_SESSION":
            if self.role == "coordinator" and self.session_phase == PHASE_WAITING:
                await self._begin_session()
        elif msg_type == "SESSION_RESET":
            await self._handle_session_reset()
        elif msg_type == "STEP_DOWN_ACK":
            await self._handle_step_down_ack(message.sender_id)
        elif msg_type == "TRACE":
        
            print(json.dumps({
                "time": time.time(),
                "node_id": sender_id,
                "event": payload.get("event"),
                "detail": payload.get("detail", {})
            }))


    async def _handle_heartbeat(self, payload: dict):
        #handle heartbeat from coordinator
        coordinator_id = payload.get("coordinator_id")
        phase = payload.get("phase", "unknown")

        self.last_heartbeat = time.time()
        self.session_phase = phase
        self.submission_deadline = payload.get("submission_deadline")
        self.voting_deadline = payload.get("voting_deadline")

        # update state server coordinator if needed
        if self.coordinator_id != coordinator_id:
            self.coordinator_id = coordinator_id

    async def _heartbeat_monitor(self):
        #monitor for heartbeat timeout and trigger election
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            if not self.running:
                break

            # check if coordinator
            if self.role == "coordinator":
                # send heartbeat
                msg = create_message(
                    "HEARTBEAT",
                    self.node_id,
                    {
                        "coordinator_id": self.node_id,
                        "phase": self.session_phase,
                        "submission_deadline": self.submission_deadline,
                        "voting_deadline": self.voting_deadline,
                    }
                )
                await self._broadcast(msg)
                continue

            # Check heartbeat timeout
            time_since_heartbeat = time.time() - self.last_heartbeat
            if time_since_heartbeat > TIMEOUT:
                emit_trace("HEARTBEAT_TIMEOUT", self.node_id, {
                    "time_since": time_since_heartbeat
                })
                await self._start_election()

    async def _start_election(self):
        #start a new election using the Modified Bully algorithm
        #the initiator sends ELECTION to all higher-ID nodes, waits for OK replies
        #then selects the highest-ID respondent (or itself if no replies) as winner, and broadcasts COORDINATOR on that node's behalf
        if self.election_state["in_progress"]:
            return

        self.current_term += 1
        self.election_state["in_progress"] = True
        self.election_state["ok_received"] = []
        self.election_state["higher_nodes_contacted"] = []

        if self.role == "coordinator":
            self.frozen = True

        emit_trace("ELECTION_SENT", self.node_id, {"term": self.current_term})

        # Identify all higher id
        higher_node_ids = [nid for nid in self.peer_ports if (nid - 8000) > self.node_id]
        self.election_state["higher_nodes_contacted"] = higher_node_ids

        if higher_node_ids:
            # send ELECTION to all higher ID 
            election_msg = create_message("ELECTION", self.node_id)
            await self._broadcast_to_ports(election_msg, higher_node_ids)

            # wait for OK replies until all contacted OR timeout
            try:
                self.election_state["timer"] = asyncio.create_task(
                    asyncio.wait_for(
                        self._wait_for_election_completion(),
                        timeout=ELECTION_TIMEOUT
                    )
                )
                await self.election_state["timer"]
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass  # Timeout or cancelled by _handle_ok - both mean election can proceed

        
            await self._determine_election_winner()
        else:
            # No higher-ID nodes, we are the winner
            emit_trace("NO_HIGHER_NODES", self.node_id, {"node_id": self.node_id})
            await self._announce_coordinator(self.node_id)

    async def _wait_for_election_completion(self):
        expected = len(self.election_state["higher_nodes_contacted"])
        while len(self.election_state["ok_received"]) < expected:
            if not self.election_state["in_progress"]:
                return  # election ended early
            await asyncio.sleep(0.1)

    async def _determine_election_winner(self):
    
        #select the highest-ID node from
       #1. all nodes that replied OK (higher-ID responders)
     #2. self (if no higher-ID nodes exist or responded)
        # guard against double-call: can be triggered by both _handle_ok and _start_election
        if not self.election_state["in_progress"]:
            return

        ok_received = self.election_state["ok_received"]

        if ok_received:
            winner_id = max(ok_received)
            emit_trace("ELECTION_COMPLETE", self.node_id, {
                "ok_received": ok_received,
                "winner": winner_id
            })
        else:
            winner_id = self.node_id
            emit_trace("ELECTION_COMPLETE", self.node_id, {
                "ok_received": [],
                "winner": winner_id,
                "reason": "no_higher_nodes_responded"
            })

        await self._announce_coordinator(winner_id)

    async def _handle_election(self, message: Message, connection: websockets.WebSocketClientProtocol):
        sender_id = message.sender_id

        emit_trace("ELECTION_RECEIVED", self.node_id, {"from": sender_id})

        # FAULT EXPERIMENT: drop ok message randomly based on environment
        drop_rate = float(os.environ.get("DROP_RATE", "0"))
        if random.random() < drop_rate:
            emit_trace("OK message dropped",self.node_id, {"to": sender_id, "drop_rate": drop_rate})
            return

        # reply OK
        ok_msg = create_message("OK", self.node_id)
        sender_port = 8000 + sender_id
        if sender_port in self.outgoing_connections:
            try:
                await self.outgoing_connections[sender_port].send(ok_msg.to_json())
            except Exception:
                del self.outgoing_connections[sender_port]
        else:
            try:
                await connection.send(ok_msg.to_json())
            except Exception:
                pass


    async def _handle_ok(self, sender_id: int):

        #records the sender's ID and wakes up the election waiter if all OKs received

        if self.election_state["in_progress"] and sender_id not in self.election_state["ok_received"]:
            self.election_state["ok_received"].append(sender_id)
            emit_trace("OK_RECEIVED", self.node_id, {"from": sender_id})

            # Check if received all expected OKs
            expected = len(self.election_state["higher_nodes_contacted"])
            if len(self.election_state["ok_received"]) >= expected:
                # Cancel the timeout timer and  determine winner
                if self.election_state["timer"]:
                    self.election_state["timer"].cancel()
                await self._determine_election_winner()

    async def _query_for_coordinator(self):
        #query all known nodes to find the coordinator.
        #recover node first sends QUERY to all higher-ID nodes
        #only if no ANSWER is received, initiate an election 


        # clear coordinator_id so we actually wait for ANSWER
        # if don't clear, _wait_for_answer() returns immediately
        self.coordinator_id = None
        self.role = "peer"

        emit_trace("QUERY_SENT", self.node_id)

        query_msg = create_message("QUERY", self.node_id)
        await self._broadcast(query_msg)

        # wait for ANSWER
        try:
            await asyncio.wait_for(self._wait_for_answer(), timeout=QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            # no coordinator found, start election
            emit_trace("COORDINATOR_LOST", self.node_id)
            await self._start_election()

    async def _wait_for_answer(self):
        while self.coordinator_id is None:
            await asyncio.sleep(0.1)

    async def _handle_query(self, message: Message, connection: websockets.WebSocketClientProtocol):
        #handle QUERY message by sending ANSWER if we know the coordinator.
        #both coordinator and peer nodes reply to QUERY if they know the coordinator
    
        sender_id = message.sender_id

        if self.coordinator_id is not None:
            answer_msg = create_message(
                "ANSWER",
                self.node_id,
                {"coordinator_id": self.coordinator_id}
            )
            try:
                await connection.send(answer_msg.to_json())
                emit_trace("ANSWER_SENT", self.node_id, {"to": sender_id})
            except Exception:
                pass

    async def _handle_answer(self, payload: dict):
    
        #if coordinator_id > self.node_id: accept and become peer
        #if coordinator_id < self.node_id: be the coordinator, don't accept

        coordinator_id = payload.get("coordinator_id")

        if not coordinator_id:
            return

        if coordinator_id > self.node_id:
            self.coordinator_id = coordinator_id
            self.role = "peer"
            emit_trace("ANSWER_RECEIVED", self.node_id, {
                "coordinator_id": coordinator_id,
                "action": "accept_higher"
            })
        elif coordinator_id < self.node_id:
            emit_trace("ANSWER_RECEIVED", self.node_id, {
                "coordinator_id": coordinator_id,
                "action": "reject_lower",
                "reason": "we_have_higher_id"

            })
            asyncio.create_task(self._start_election())

    async def _handle_step_down_ack(self, sender_id: int):
        emit_trace("STEP_DOWN_RECIEVED", self.node_id, {"from": sender_id})
        self._step_down_ack_event.set()

    async def _announce_coordinator(self, winner_id: int):
        #broadcasts COORDINATOR message on behalf of the winner (which may be self).
        #if the winner is self, also starts the coordinator session


        self.election_state["in_progress"] = True

        emit_trace("COORDINATOR_ELECTED", self.node_id, {
            "term": self.current_term,
            "winner_id": winner_id
        })

        # broadcast COORDINATOR message with winner's ID (sender is the announcer)
        coordinator_msg = create_message(
            "COORDINATOR",
            self.node_id,
            {"coordinator_id": winner_id}

        )

        await self._broadcast(coordinator_msg)

        # if it is the winner, become coordinator
        if winner_id == self.node_id:
            self._step_down_ack_event.clear()

            # wait for old coordinator to step down
            try:
                await asyncio.wait_for(self._step_down_ack_event.wait(),
                                       timeout=ELECTION_TIMEOUT)
                emit_trace("STEP_DOWN_CONFIRMED", self.node_id)
            except asyncio.TimeoutError:
                # old coordinator likely crashed, proceed anyway
                emit_trace("STEP_DOWN_TIMEOUT", self.node_id)
            finally:
                self.election_state["in_progress"] = False

            self.coordinator_id = self.node_id
            self.role = "coordinator"

            # Update state server
            await self._update_coordinator_to_server(self.node_id)
            # Resume or start session
            await self._resume_session()
        else:
            # other node won - update local state
            self.coordinator_id = winner_id
            emit_trace("COORDINATOR_ACCEPTED", self.node_id, {
                "coordinator_id": winner_id
            })
            self.election_state["in_progress"] = False


    async def _handle_coordinator(self, message: Message, connection: websockets.WebSocketClientProtocol = None):
        #handle COORDINATOR broadcast.    

        #the sender of this message is the node that initiated/broadcast the election
        #the coordinator_id in payload is the winner (may be different from sender).

        drop_rate = float(os.environ.get("DROP_RATE", "0"))
        if random.random() < drop_rate:
            emit_trace("COORDINATOR message dropped", self.node_id, {"from": message.sender_id,
                                                                     "drop_rate": drop_rate})
            return

        payload = message.payload
        new_coordinator_id = payload.get("coordinator_id")
        sender_id = message.sender_id

        emit_trace("COORDINATOR_BROADCAST", self.node_id, {
            "coordinator_id": new_coordinator_id,
            "sender": sender_id
        })

        # update coordinator ID
        self.coordinator_id = new_coordinator_id
        self.election_state["in_progress"] = False

        # if it is the winner, become coordinator
        if new_coordinator_id == self.node_id:
            self.role = "coordinator"
            # update state server
            await self._update_coordinator_to_server(self.node_id)
            # resume or start session
            await self._resume_session()

        else:
            was_coordinator = (self.role == "coordinator")
            self.role = "peer"
            await self._update_coordinator_to_server(new_coordinator_id)
            self.election_state["in_progress"] = False

            if was_coordinator:
                if self._session_task and not self._session_task.done():
                    self._session_task.cancel()
                    self._session_task = None

                ack_msg = create_message("STEP_DOWN_ACK", self.node_id)
                sender_port = 8000 + sender_id
                if sender_port in self.outgoing_connections:
                    try:
                        await self.outgoing_connections[sender_port].send(ack_msg.to_json())
                        emit_trace("STEP_DOWN_ACK_SENT", self.node_id, {"to":sender_id})
                    except Exception:
                        pass
                elif connection:
                    try:
                        await connection.send(ack_msg.to_json())
                        emit_trace("STEP_DOWN_ACK_SENT", self.node_id, {"to":sender_id})
                    except Exception:
                        pass

        # if frozen (thought it was coordinator), unfreeze
        if self.frozen:
            self.frozen = False
            emit_trace("UNFROZEN", self.node_id)

    async def _resume_session(self):
        #resume session as the new coordinator
        await self._sync_with_state_server()

        emit_trace("SESSION_RESUMED", self.node_id, {
            "phase": self.session_phase,
            "questions_count": len(self.questions),
            "votes_count": sum(len(v) for v in self.votes.values())
        })

        # broadcast SESSION_RESUME to all peers
        resume_msg = create_message(
            "SESSION_RESUME",
            self.node_id,
            {
                "phase": self.session_phase,
                "questions": self.questions,
                "votes": self.votes
            }
        )
        await self._broadcast(resume_msg)

        # calculate remaining time and resume appropriate phase
        current_time = time.time()

        if self.session_phase == PHASE_SUBMISSION and self.submission_deadline:
            remaining = self.submission_deadline - current_time
            if remaining > 0:
                self._session_task = asyncio.create_task(self._run_submission_phase(remaining))
            else:
                # submission phase should have ended, advance to voting
                await self._advance_to_voting()
        elif self.session_phase == PHASE_SUBMISSION and not self.submission_deadline:
            self.session_phase = PHASE_WAITING
            emit_trace("WAITING_FOR_START", self.node_id)
        elif self.session_phase == PHASE_VOTING and self.voting_deadline:
            remaining = self.voting_deadline - current_time
            if remaining > 0:
                self._session_task = asyncio.create_task(self._run_voting_phase(remaining))
            else:
                # voting phase should have ended, close session
                await self._close_session()
        elif self.session_phase == PHASE_CLOSED:
            await self._broadcast_final_results()
        elif self.session_phase == PHASE_WAITING:
            emit_trace("WAITING_FOR_START", self.node_id)

    async def _handle_session_resume(self, payload: dict):
        #handle SESSION_RESUME message from new coordinator.
        phase = payload.get("phase")
        questions = payload.get("questions", [])
        votes = payload.get("votes", {})

        self.session_phase = phase
        self.questions = questions
        self.votes = votes
        self.frozen = False

        emit_trace("SESSION_RESUMED", self.node_id, {
            "phase": phase,
            "questions_count": len(questions)
        })



    async def _handle_session_update(self, payload: dict):
        phase = payload.get("phase")
        self.session_phase = phase
        emit_trace("SESSION_UPDATE", self.node_id, {"phase": phase})

    async def _election_monitor(self):
        #monitor for election needs.
        while self.running:
            await asyncio.sleep(0.5)

            if not self.running:
                break

            await self._connect_to_peers()

            # check if should trigger election (no coordinator and not in election)
            if self.coordinator_id is None and not self.election_state["in_progress"]:
                await self._start_election()



    # coordinator session management
    async def _run_submission_phase(self, duration: float):
        #run the submission phase as coordinator
        self.session_phase = PHASE_SUBMISSION
        self.submission_deadline = time.time() + duration

        emit_trace("SUBMISSION_STARTED", self.node_id, {
            "duration": duration,
            "deadline": self.submission_deadline
        })

        # update state server
        await self._update_phase_to_server(
            phase=PHASE_SUBMISSION,
            submission_deadline=self.submission_deadline
        )


        # wait for submission deadline
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            return


        await self._advance_to_voting()

    async def _advance_to_voting(self):
        self.frozen = False
        self.session_phase = PHASE_VOTING
        self.voting_deadline = time.time() + VOTING_DURATION

        await self._update_phase_to_server(
            phase=PHASE_VOTING,
            voting_deadline=self.voting_deadline
        )
        await self._sync_with_state_server()

        questions_msg = create_message(
            "QUESTIONS",
            self.node_id,
            {"questions": self.questions}
        )
        await self._broadcast(questions_msg)

        emit_trace("PHASE_ADVANCED", self.node_id, {
            "from": PHASE_SUBMISSION,
            "to": PHASE_VOTING,
            "questions_count": len(self.questions)
        })

        # send session update
        update_msg = create_message(
            "SESSION_UPDATE",
            self.node_id,
            {"phase": PHASE_VOTING}
        )
        await self._broadcast(update_msg)

        # start voting phase
        self._session_task = asyncio.create_task(self._run_voting_phase(VOTING_DURATION))

    async def _run_voting_phase(self, duration: float):
        #run the voting phase 
        #record the voting deadline in case the coodinator crashes, can still work
        self.voting_deadline = time.time() + duration

        emit_trace("VOTING_STARTED", self.node_id, {
            "duration": duration,
            "deadline": self.voting_deadline
        })

        # start periodic result broadcasts
        self.vote_broadcast_task = asyncio.create_task(self._periodic_result_broadcast())

        # wait for voting deadline
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            self.vote_broadcast_task.cancel()
            return

     
        await self._close_session()

    async def _periodic_result_broadcast(self):
        #periodically broadcast live vote counts every 5 seconds
        while self.session_phase == PHASE_VOTING and self.running:
            await asyncio.sleep(5)  
            if self.session_phase == PHASE_VOTING:
                await self._broadcast_live_results()


    async def _broadcast_live_results(self):
        rankings = self._calculate_rankings()
        self.latest_rankings = rankings
        result_msg = create_message(
            "RESULT",
            self.node_id,
            {"rankings": rankings}
        )
        await self._broadcast(result_msg)

    async def _close_session(self):
        self.frozen = False
        self.session_phase = PHASE_CLOSED

        if self.vote_broadcast_task:
            self.vote_broadcast_task.cancel()

        emit_trace("SESSION_CLOSED", self.node_id)

        await self._update_phase_to_server(PHASE_CLOSED)
        close_msg = create_message("SESSION_UPDATE", self.node_id, {"phase": PHASE_CLOSED})
        await self._broadcast(close_msg)
        await self._broadcast_final_results()

        await asyncio.sleep(10)
        await self._reset_session()

    async def _broadcast_final_results(self):
        rankings = self._calculate_rankings()
        self.latest_rankings = rankings
        result_msg = create_message(
            "RESULT",
            self.node_id,
            {"rankings": rankings}
        )
        await self._broadcast(result_msg)
        emit_trace("FINAL_RESULT_BROADCAST", self.node_id, {
            "rankings": rankings
        })

    def _calculate_rankings(self) -> list:
        #calculate vote rankings sorted by vote count descending

        rankings = []

        for question in self.questions:
            qid = question["id"]
            vote_count = len(self.votes.get(qid, []))
            rankings.append({
                "id": qid,
                "text": question["text"],
                "votes": vote_count
            })

        # sort by votes descending
        rankings.sort(key=lambda x: x["votes"], reverse=True)
        return rankings




    # peer session actions

    async def submit_question(self, question: str):
        #submit a question during submission phase
        if self.session_phase != PHASE_SUBMISSION:
            emit_trace("SUBMIT_REJECTED", self.node_id, {
                "reason": "not in submission phase",
                "phase": self.session_phase
            })
            return False


        if self.has_submitted:
            emit_trace("SUBMIT_REJECTED", self.node_id, {
                "reason": "already submitted"
            })
            return False

        # send SUBMIT message to coordinator
        if self.role == "coordinator":
            result = await self._add_question_to_server(question, self.node_id)
            if result:
                await self._sync_with_state_server()
        else:
            submit_msg = create_message("SUBMIT", self.node_id, {"question": question})
            await self._broadcast(submit_msg)
        self.has_submitted = True

        emit_trace("SUBMIT_SENT", self.node_id, {"question": question[:50]})
        return True

    async def vote(self, question_ids: list):
        #vote for questions during voting phase
        if self.session_phase != PHASE_VOTING:
            emit_trace("VOTE_REJECTED", self.node_id, {
                "reason": "not in voting phase",
                "phase": self.session_phase
            })
            return False

        if self.has_voted:
            emit_trace("VOTE_REJECTED", self.node_id, {
                "reason": "already voted"
            })
            return False

        if not self.questions:
            await self._sync_with_state_server()

        valid_ids = [qid for qid in question_ids if qid in [q["id"] for q in self.questions]]
        if not valid_ids:
            emit_trace("VOTE_REJECTED", self.node_id, {
                "reason": "no valid question IDs"
            })
            return False

        if self.role == "coordinator":
            for qid in valid_ids:
                await self._add_vote_to_server(qid, self.node_id)
            await self._sync_with_state_server()
        else:
            vote_msg = create_message(
                "VOTE",
                self.node_id,
                {"question_ids": valid_ids}
            )
            await self._broadcast(vote_msg)
        self.has_voted = True

        emit_trace("VOTE_SENT", self.node_id, {"question_ids": valid_ids})
        return True




    # coordinator Message Handlers 

    async def _handle_submit(self, message: Message):
        #handle SUBMIT message from peer (coordinator only).
        if self.role != "coordinator" or self.frozen:
            return

        if self.session_phase != PHASE_SUBMISSION:
            return

        sender_id = message.sender_id
        question = message.payload.get("question")

        if not question:
            return

        emit_trace("SUBMIT_RECEIVED", self.node_id, {
            "question": question[:50],
            "from": sender_id
        })

        # add to state server (handles deduplication)
        result = await self._add_question_to_server(question, sender_id)

        if result:
            await self._sync_with_state_server()


    async def _handle_vote(self, message: Message):
        #handle VOTE message from peer (coordinator only).
        if self.role != "coordinator" or self.frozen:
            return

        if self.session_phase != PHASE_VOTING:
            return

        sender_id = message.sender_id
        question_ids = message.payload.get("question_ids", [])

        emit_trace("VOTE_RECEIVED", self.node_id, {
            "question_ids": question_ids,
            "from": sender_id
        })

        # process each vote
        for qid in question_ids:
            if qid in [q["id"] for q in self.questions]:
                await self._add_vote_to_server(qid, sender_id)

        await self._sync_with_state_server()

    async def _handle_questions(self, payload: dict):
        #handle QUESTIONS message from coordinator (peer only).
        questions = payload.get("questions", [])
        self.questions = questions

        emit_trace("QUESTIONS_RECEIVED", self.node_id, {
            "count": len(questions)
        })

    async def _handle_result(self, payload: dict):
        rankings = payload.get("rankings", [])
        self.latest_rankings = rankings

        emit_trace("RESULT_RECEIVED", self.node_id, {
            "rankings": rankings
        })




    # late join handling

    async def check_and_handle_late_join(self):
        #check if joining late and handle accordingly.
        current_time = time.time()

        if self.session_phase == PHASE_CLOSED:
            # can only see final results, already handled via sync
            emit_trace("LATE_JOIN_CLOSED", self.node_id)
        elif self.session_phase == PHASE_VOTING:
            # can vote but not submit
            if self.submission_deadline and current_time > self.submission_deadline:
                emit_trace("LATE_JOIN_VOTING", self.node_id)
        elif self.session_phase == PHASE_SUBMISSION:
            if self.submission_deadline and current_time > self.submission_deadline:
                # missed submission window
                emit_trace("LATE_JOIN_MISSED_SUBMISSION", self.node_id)


    # ── Session lifecycle ──

    async def _begin_session(self):
        if self.session_phase != PHASE_WAITING:
            return
        emit_trace("SESSION_STARTED", self.node_id)
        self._session_task = asyncio.create_task(self._run_submission_phase(SUBMISSION_DURATION))
        update_msg = create_message("SESSION_UPDATE", self.node_id, {"phase": PHASE_SUBMISSION})
        await self._broadcast(update_msg)

    async def _reset_session(self):
        if self.role != "coordinator":
            return
        self.session_phase = PHASE_WAITING
        self.has_submitted = False
        self.has_voted = False
        self.questions = []
        self.votes = {}
        self.latest_rankings = []
        self.submission_deadline = None
        self.voting_deadline = None
        async with aiohttp.ClientSession() as session:
            await session.post(f"{STATE_SERVER_URL}/reset")
        await self._update_coordinator_to_server(self.node_id)
        reset_msg = create_message("SESSION_RESET", self.node_id)
        await self._broadcast(reset_msg)
        emit_trace("SESSION_RESET", self.node_id)

    async def _handle_session_reset(self):
        self.session_phase = PHASE_WAITING
        self.has_submitted = False
        self.has_voted = False
        self.questions = []
        self.votes = {}
        self.latest_rankings = []
        self.submission_deadline = None
        self.voting_deadline = None
        emit_trace("SESSION_RESET", self.node_id)

    # ── Web UI server ──

    async def _start_web_server(self):
        app = web.Application()
        app.router.add_get("/", self._handle_web_index)
        app.router.add_get("/ws", self._handle_web_ws)
        app.router.add_post("/start", self._handle_web_start)
        app.router.add_post("/submit", self._handle_web_submit)
        app.router.add_post("/vote", self._handle_web_vote)
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        app.router.add_static("/static", static_dir)

        runner = web.AppRunner(app)
        await runner.setup()
        web_port = 9000 + self.node_id
        site = web.TCPSite(runner, "localhost", web_port)
        await site.start()
        emit_trace("WEB_SERVER_STARTED", self.node_id, {"port": web_port})
        asyncio.create_task(self._ws_status_loop())

    async def _handle_web_index(self, request):
        path = os.path.join(os.path.dirname(__file__), "static", "voting.html")
        return web.FileResponse(path)

    async def _handle_web_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            async for _ in ws:
                pass
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def _ws_broadcast(self, data: dict):
        msg = json.dumps(data)
        closed = []
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                closed.append(ws)
        for ws in closed:
            self._ws_clients.discard(ws)

    async def _ws_status_loop(self):
        while self.running:
            await asyncio.sleep(1)
            now = time.time()
            if self.session_phase == PHASE_SUBMISSION and self.submission_deadline:
                time_left = max(0, self.submission_deadline - now)
            elif self.session_phase == PHASE_VOTING and self.voting_deadline:
                time_left = max(0, self.voting_deadline - now)
            else:
                time_left = None
            status = {
                "type": "status",
                "node_id": self.node_id,
                "role": self.role,
                "coordinator_id": self.coordinator_id,
                "phase": self.session_phase,
                "connected_peers": len(self.outgoing_connections),
                "total_peers": len(self.peer_ports),
                "has_submitted": self.has_submitted,
                "has_voted": self.has_voted,
                "time_left": time_left,
            }
            await self._ws_broadcast(status)
            if self.questions:
                vote_map = {}
                for r in self.latest_rankings:
                    vote_map[r["id"]] = r["votes"]
                q_list = []
                for q in self.questions:
                    qid = q["id"]
                    votes = vote_map.get(qid, len(self.votes.get(qid, [])))
                    q_list.append({
                        "id": qid,
                        "text": q["text"],
                        "submitted_by": q.get("submitted_by"),
                        "votes": votes
                    })
                await self._ws_broadcast({"type": "questions", "questions": q_list})
            rankings = self.latest_rankings if self.latest_rankings else self._calculate_rankings()
            if rankings:
                await self._ws_broadcast({"type": "results", "rankings": rankings})

    def _push_log(self, ts: str, event: str, detail: dict = None):
        data = {"type": "log", "time": ts, "event": event, "detail": detail}
        for ws in list(self._ws_clients):
            try:
                asyncio.ensure_future(ws.send_str(json.dumps(data)))
            except Exception:
                self._ws_clients.discard(ws)

    async def _handle_web_start(self, request):
        if self.session_phase != PHASE_WAITING:
            return web.json_response({"error": "not in waiting phase"}, status=400)
        if self.role == "coordinator":
            await self._begin_session()
        else:
            start_msg = create_message("START_SESSION", self.node_id)
            if self.coordinator_id and (8000 + self.coordinator_id) in self.outgoing_connections:
                try:
                    await self.outgoing_connections[8000 + self.coordinator_id].send(start_msg.to_json())
                except Exception:
                    return web.json_response({"error": "failed to reach coordinator"}, status=503)
            else:
                await self._broadcast(start_msg)
        return web.json_response({"status": "ok"})

    async def _handle_web_submit(self, request):
        data = await request.json()
        question = data.get("question", "").strip()
        if not question:
            return web.json_response({"error": "empty question"}, status=400)
        ok = await self.submit_question(question)
        if ok:
            return web.json_response({"status": "ok"})
        return web.json_response({"error": "submit failed"}, status=400)

    async def _handle_web_vote(self, request):
        data = await request.json()
        question_ids = data.get("question_ids", [])
        ok = await self.vote(question_ids)
        if ok:
            return web.json_response({"status": "ok"})
        return web.json_response({"error": "vote failed"}, status=400)


#main entry point for running a node.

async def main_async():
    
    parser = argparse.ArgumentParser(description="Distributed Voting Application Node")
    parser.add_argument("--id", type=int, required=True, help="Unique node ID")
    parser.add_argument("--port", type=int, required=True, help="Port for this node")
    parser.add_argument("--peers", type=str, required=True, help="Comma-separated list of peer ports")

    args = parser.parse_args()
    peer_ports = [int(p.strip()) for p in args.peers.split(",")]

    node = Node(args.id, args.port, peer_ports)

    try:
        await node.start()
        while node.running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await node.stop()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

