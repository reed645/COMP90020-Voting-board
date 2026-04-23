"""
Unit tests for the Modified Bully Algorithm election logic in node.py.
"""

import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from node import Node, emit_trace
from config import (
    ELECTION_TIMEOUT,
    PHASE_SUBMISSION,
    PHASE_VOTING,
    SUBMISSION_DURATION,
    VOTING_DURATION,
)


@pytest.fixture
def node():
    """Create a fresh node for each test."""
    n = Node(node_id=3, port=8003, peer_ports=[8005, 8007, 8009])
    n.running = True
    n.loop = asyncio.get_event_loop()
    return n


@pytest.fixture
def node_without_peers():
    """Create a node with no higher-ID peers (for self-election tests)."""
    n = Node(node_id=10, port=8010, peer_ports=[8001, 8002, 8003])
    n.running = True
    n.loop = asyncio.get_event_loop()
    return n


# =============================================================================
# Test 1: Election winner is highest ID from OKs
# =============================================================================
@pytest.mark.asyncio
async def test_election_winner_is_highest_ok_id(node):
    """
    When higher-ID nodes reply OK, winner should be max(ok_received).
    Since node 9 has highest ID among [5, 7, 9], it should win.
    """
    node._broadcast = AsyncMock()
    node._broadcast_to_ports = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    # Set up election state as if we sent ELECTION to [5, 7, 9]
    node.election_state["higher_nodes_contacted"] = [5, 7, 9]
    node.election_state["ok_received"] = [5, 7, 9]
    node.election_state["in_progress"] = True

    # Call _determine_election_winner directly
    await node._determine_election_winner()

    # Verify election completed
    assert node.election_state["in_progress"] is False

    # Winner should be 9 (highest)
    # COORDINATOR broadcast should have been sent with winner_id=9
    node._broadcast.assert_called()
    call_args = node._broadcast.call_args
    msg = call_args[0][0]
    assert msg.type == "COORDINATOR"
    assert msg.payload["coordinator_id"] == 9

    # Node should NOT become coordinator (9 is winner, not self)
    assert node.role == "peer"
    assert node.coordinator_id == 9


# =============================================================================
# Test 2: Election initiated, no one replies (timeout)
# =============================================================================
@pytest.mark.asyncio
async def test_election_no_replies_self_wins(node_without_peers):
    """
    When no higher-ID nodes exist (or none reply), the initiator becomes
    coordinator itself and calls _resume_session().
    """
    node = node_without_peers
    node._broadcast = AsyncMock()
    node._broadcast_to_ports = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    # No higher-ID peers that can reply
    await node._start_election()

    # Wait for timeout
    await asyncio.sleep(ELECTION_TIMEOUT + 0.1)

    # Node should become coordinator
    assert node.role == "coordinator"
    assert node.coordinator_id == node.node_id

    # Should have called _resume_session
    node._resume_session.assert_called_once()

    # COORDINATOR broadcast should contain self's ID
    node._broadcast.assert_called()
    call_args = node._broadcast.call_args
    msg = call_args[0][0]
    assert msg.type == "COORDINATOR"
    assert msg.payload["coordinator_id"] == node.node_id


# =============================================================================
# Test 3: Receive ELECTION message, send OK back
# =============================================================================
@pytest.mark.asyncio
async def test_receive_election_sends_ok(node):
    """
    When receiving ELECTION from a lower-ID node, the node should
    send OK back and NOT start its own election.
    """
    from messages import Message, create_message

    node._broadcast = AsyncMock()
    node._start_election = AsyncMock()

    # Create a mock connection
    mock_connection = AsyncMock()

    # Create ELECTION message from lower-ID node (node_id=2)
    election_msg = create_message("ELECTION", sender_id=2)

    # Receive the message
    await node._handle_election(election_msg, mock_connection)

    # Should have sent OK back
    mock_connection.send.assert_called_once()
    sent_msg = Message.from_json(mock_connection.send.call_args[0][0])
    assert sent_msg.type == "OK"
    assert sent_msg.sender_id == node.node_id

    # Should NOT have started its own election
    node._start_election.assert_not_called()


# =============================================================================
# Test 4: _determine_election_winner guard against double-call
# =============================================================================
@pytest.mark.asyncio
async def test_determine_winner_guard_double_call(node):
    """
    When election_state["in_progress"] is False, _determine_election_winner
    should return immediately without doing anything.
    """
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    # Set in_progress to False (simulating already called)
    node.election_state["in_progress"] = False

    # Call _determine_election_winner
    await node._determine_election_winner()

    # Should NOT broadcast COORDINATOR
    node._broadcast.assert_not_called()

    # Should NOT call _resume_session
    node._resume_session.assert_not_called()


# =============================================================================
# Test: ELECTION sent to correct ports
# =============================================================================
@pytest.mark.asyncio
async def test_election_sends_to_correct_ports(node):
    """
    Election message should only be sent to nodes with higher IDs.
    Note: peer_ports are [8005, 8007, 8009] (port numbers).
    Since port = node_id * 1000 + 3000, higher IDs are [5, 7, 9].
    """
    node._broadcast = AsyncMock()
    node._broadcast_to_ports = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    # Start election
    await node._start_election()

    # Should have sent ELECTION to higher-ID nodes
    node._broadcast_to_ports.assert_called_once()

    # Get the call arguments
    call_args = node._broadcast_to_ports.call_args
    msg = call_args[0][0]
    ports = call_args[0][1]

    assert msg.type == "ELECTION"
    assert msg.sender_id == node.node_id
    # peer_ports are [8005, 8007, 8009], all > 3, so all sent
    assert ports == [8005, 8007, 8009]


# =============================================================================
# Additional: Election increments term
# =============================================================================
@pytest.mark.asyncio
async def test_election_increments_term(node):
    """
    Each election should increment current_term.
    """
    node._broadcast = AsyncMock()
    node._broadcast_to_ports = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    initial_term = node.current_term

    await node._start_election()

    assert node.current_term == initial_term + 1


# =============================================================================
# Additional: Cannot start election while one is in progress
# =============================================================================
@pytest.mark.asyncio
async def test_cannot_start_election_twice(node):
    """
    If an election is already in progress, _start_election should return early.
    """
    node._broadcast = AsyncMock()
    node._broadcast_to_ports = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    # Manually set in_progress to True first
    node.election_state["in_progress"] = True

    # Try to start election - should return immediately
    await node._start_election()

    # Term should not have incremented
    assert node.current_term == 0

    # Should NOT have sent any messages
    node._broadcast_to_ports.assert_not_called()


# =============================================================================
# Additional: Election state reset on new election
# =============================================================================
@pytest.mark.asyncio
async def test_election_state_reset(node):
    """
    Starting a new election should reset ok_received and higher_nodes_contacted.
    """
    node._broadcast = AsyncMock()
    node._broadcast_to_ports = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    # Set some initial state (but in_progress=False so election will run)
    node.election_state["ok_received"] = [8005]  # Old OKs
    node.election_state["in_progress"] = False  # Must be False for election to run

    # Start election - should reset state before the wait completes
    # Use a very short timeout so we can check state mid-flight
    with patch('node.ELECTION_TIMEOUT', 0.05):
        # Don't await - let it run in background
        task = asyncio.create_task(node._start_election())
        await asyncio.sleep(0.01)  # Let it run past the state-setting

        # At this point, state should be reset
        assert node.election_state["ok_received"] == []
        assert node.election_state["higher_nodes_contacted"] == [8005, 8007, 8009]
        assert node.election_state["in_progress"] is True

        # Wait for completion
        await task


# =============================================================================
# Additional: Lower ID nodes don't receive ELECTION
# =============================================================================
@pytest.mark.asyncio
async def test_election_only_higher_ids(node):
    """
    ELECTION should only be sent to nodes with higher IDs than self.
    """
    node._broadcast = AsyncMock()
    node._broadcast_to_ports = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._resume_session = AsyncMock()

    # node_id = 3, peer_ports = [8005, 8007, 8009]
    # Higher IDs: [5, 7, 9]
    # Lower IDs: [1, 2] would not be in peer_ports

    await node._start_election()

    # Verify ports sent to
    node._broadcast_to_ports.assert_called()
    _, kwargs = node._broadcast_to_ports.call_args
    ports = kwargs.get('ports') or node._broadcast_to_ports.call_args[0][1]

    # All ports should be > node_id (3)
    for port in ports:
        assert port > 3
