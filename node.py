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

from config import (
    HEARTBEAT_INTERVAL,
    TIMEOUT,
    ELECTION_TIMEOUT,
    QUERY_TIMEOUT,
    STATE_SERVER_URL,
    SUBMISSION_DURATION,
    VOTING_DURATION,
    PHASE_SUBMISSION,
    PHASE_VOTING,
    PHASE_CLOSED,
)
from messages import Message, create_message


def emit_trace(event: str, node_id: int, detail: dict = None):
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    detail_str = "  " + ", ".join(f"{k}={v}" for k, v in (detail or {}).items())
    print(f"[{ts}] Node {node_id} | {event:<35} |{detail_str}")


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
        self.session_phase = PHASE_SUBMISSION
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

        # unique identifier for this node instance (in case the node crush)
        self.instance_id = str(uuid.uuid4())[:8]


    async def start(self):
        self.loop = asyncio.get_event_loop()
        self.running = True

        # connect to state server and get initial state
        await self._sync_with_state_server()

        self.websocket_server = await serve(
      self._handle_client,
      host="localhost",
      port=self.port
  )


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

        # Check if we should become coordinator immediately (no coordinator exists)
        if self.coordinator_id is None:
            asyncio.create_task(self._start_election())

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
                        self.session_phase = state.get("phase", PHASE_SUBMISSION)
                        self.questions = state.get("questions", [])
                        self.votes = state.get("votes", {})
                        self.submission_deadline = state.get("submission_deadline")
                        self.voting_deadline = state.get("voting_deadline")

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
                    emit_trace("PEER_CONNECTED", self.node_id, {"port": port})
                except Exception as e:
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
                    {"coordinator_id": self.node_id, "phase": self.session_phase}
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

        self.election_state["in_progress"] = False

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
            await self._start_election()

    async def _announce_coordinator(self, winner_id: int):
        #broadcasts COORDINATOR message on behalf of the winner (which may be self).
        #if the winner is self, also starts the coordinator session


        self.election_state["in_progress"] = False

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

        # Check for possible split-brain
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{STATE_SERVER_URL}/state") as resp:
                    if resp.status == 200:
                        current = await resp.json()
                        existing = current.get("coordinator_id")
                        if existing is not None and existing != self.node_id:
                            emit_trace("SPLIT_BRAIN_DETECTED", self.node_id, {
                                "self_claims": self.node_id,
                                "server_has": existing
                            })
        except Exception:
            pass
        await self._broadcast(coordinator_msg)

        # if it is the winner, become coordinator
        if winner_id == self.node_id:
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
            self.role = "peer"
            await self._update_coordinator_to_server(new_coordinator_id)

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
                asyncio.create_task(self._run_submission_phase(remaining))
            else:
                # submission phase should have ended, advance to voting
                await self._advance_to_voting()
        elif self.session_phase == PHASE_SUBMISSION and not self.submission_deadline:
            # begin fresh submission phase
            asyncio.create_task(self._run_submission_phase(SUBMISSION_DURATION))
        elif self.session_phase == PHASE_VOTING and self.voting_deadline:
            remaining = self.voting_deadline - current_time
            if remaining > 0:
                asyncio.create_task(self._run_voting_phase(remaining))
            else:
                # voting phase should have ended, close session
                await self._close_session()
        elif self.session_phase == PHASE_CLOSED:

            await self._broadcast_final_results()

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
        #handle SESSION_UPDATE message (phase change notification).
        phase = payload.get("phase")
        self.session_phase = phase

        emit_trace("SESSION_UPDATE", self.node_id, {"phase": phase})

        if phase == PHASE_VOTING:
            # check if missed questions
            if not self.questions:
                await self._sync_with_state_server()

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
        #advance from submission phase to voting phase.
        self.frozen = False
        emit_trace("UNFROZEN", self.node_id)

        self.session_phase = PHASE_VOTING
        self.voting_deadline = time.time() + VOTING_DURATION

        # deduplicate questions by content (should already be done on state server)
        await self._sync_with_state_server()

        # broadcast questions list
        questions_msg = create_message(
            "QUESTIONS",
            self.node_id,
            {"questions": self.questions}
        )
        await self._broadcast(questions_msg)

        # update state server
        await self._update_phase_to_server(
            phase=PHASE_VOTING,
            voting_deadline=self.voting_deadline
        )

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
        asyncio.create_task(self._run_voting_phase(VOTING_DURATION))

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
        #broadcast current vote counts
        rankings = self._calculate_rankings()
        result_msg = create_message(
            "RESULT",
            self.node_id,
            {"rankings": rankings}
        )
        await self._broadcast(result_msg)

        emit_trace("RESULT_BROADCAST", self.node_id, {
            "rankings": rankings
        })

    async def _close_session(self):
        #close the session and broadcast final results
        self.frozen = False
        self.session_phase = PHASE_CLOSED

        if self.vote_broadcast_task:
            self.vote_broadcast_task.cancel()

        emit_trace("SESSION_CLOSED", self.node_id)

        # update state server
        await self._update_phase_to_server(PHASE_CLOSED)

        # broadcast final results
        await self._broadcast_final_results()

    async def _broadcast_final_results(self):
        #broadcast the final vote rankings
        rankings = self._calculate_rankings()
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

        # filter out invalid question ids
        valid_ids = [qid for qid in question_ids if qid in [q["id"] for q in self.questions]]
        if not valid_ids:
            emit_trace("VOTE_REJECTED", self.node_id, {
                "reason": "no valid question IDs"
            })
            return False

        # send VOTE message to coordinator 
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
        #handle RESULT message (for all nodes).
        rankings = payload.get("rankings", [])

        emit_trace("RESULT_RECEIVED", self.node_id, {
            "rankings": rankings
        })

        # print results for visibility
        print(f"\n{'='*60}")
        print(f"RESULTS - Node {self.node_id}")
        print('='*60)
        for i, item in enumerate(rankings, 1):
            print(f"  {i}. {item['text'][:50]} - {item['votes']} votes")
        print('='*60 + "\n")




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

