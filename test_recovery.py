"""
Unit tests for the recovery/query-before-elect logic in node.py.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from node import Node
from config import (
    QUERY_TIMEOUT,
    ELECTION_TIMEOUT,
    PHASE_SUBMISSION,
    PHASE_VOTING,
)


@pytest.fixture
def node():
    """Create a fresh node for each test."""
    n = Node(node_id=3, port=8003, peer_ports=[8005, 8007, 8009])
    n.running = True
    n.loop = asyncio.get_event_loop()
    return n


@pytest.fixture
def high_node():
    """Create a node with higher ID for testing coordinator comparisons."""
    n = Node(node_id=9, port=8009, peer_ports=[8003, 8005, 8007])
    n.running = True
    n.loop = asyncio.get_event_loop()
    return n


# =============================================================================
# Test 5: Recover, receive ANSWER with higher coordinator_id
# =============================================================================
@pytest.mark.asyncio
async def test_recover_answer_higher_coordinator(node):
    """
    When recovering node receives ANSWER with coordinator_id > self.node_id,
    it should accept the coordinator and NOT start an election.
    """
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._start_election = AsyncMock()
    node._sync_with_state_server = AsyncMock()

    # Simulate ANSWER with higher coordinator_id (9 > 3)
    await node._handle_answer({"coordinator_id": 9})

    # Node should accept the coordinator
    assert node.coordinator_id == 9
    assert node.role == "peer"

    # Should NOT have started an election
    node._start_election.assert_not_called()


# =============================================================================
# Test 6: Recover, receive ANSWER with lower coordinator_id
# =============================================================================
@pytest.mark.asyncio
async def test_recover_answer_lower_coordinator_starts_election(high_node):
    """
    When higher-ID node receives ANSWER with lower coordinator_id,
    it should start an election (it's the highest ID).
    """
    node = high_node  # node_id = 9
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._start_election = AsyncMock()

    # Simulate ANSWER with lower coordinator_id (5 < 9)
    await node._handle_answer({"coordinator_id": 5})

    # Node should have started an election
    node._start_election.assert_called()


# =============================================================================
# Test 7: Recover, QUERY timeout
# =============================================================================
@pytest.mark.asyncio
async def test_recover_query_timeout_starts_election(node):
    """
    When QUERY times out (no ANSWER received), _query_for_coordinator should
    start an election to find/become coordinator.

    Note: We test _query_for_coordinator directly because recover() has
    complex dependencies on WebSocket setup that are hard to mock cleanly.
    """
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._start_election = AsyncMock()
    node._sync_with_state_server = AsyncMock()

    # Start _query_for_coordinator in background
    query_task = asyncio.create_task(node._query_for_coordinator())

    # Wait for QUERY timeout + buffer
    await asyncio.sleep(QUERY_TIMEOUT + 0.5)

    # Should have started an election
    node._start_election.assert_called()

    # Clean up
    query_task.cancel()
    try:
        await query_task
    except asyncio.CancelledError:
        pass


# =============================================================================
# Test 8: _query_for_coordinator clears coordinator_id
# =============================================================================
@pytest.mark.asyncio
async def test_query_clears_coordinator_id(node):
    """
    _query_for_coordinator should clear coordinator_id to None so that
    _wait_for_answer actually waits for a new ANSWER.
    """
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._sync_with_state_server = AsyncMock()
    node._start_election = AsyncMock()

    # Set coordinator_id before calling
    node.coordinator_id = 5

    # Start query in background
    query_task = asyncio.create_task(node._query_for_coordinator())

    # Give it a moment to start
    await asyncio.sleep(0.05)

    # coordinator_id should be cleared
    assert node.coordinator_id is None

    # Role should be peer
    assert node.role == "peer"

    # Clean up
    query_task.cancel()
    try:
        await query_task
    except asyncio.CancelledError:
        pass


# =============================================================================
# Additional: _handle_answer ignores invalid coordinator_id
# =============================================================================
@pytest.mark.asyncio
async def test_handle_answer_ignores_none(node):
    """
    _handle_answer should ignore ANSWER with None coordinator_id.
    """
    node._broadcast = AsyncMock()
    node._start_election = AsyncMock()

    # Receive ANSWER with None coordinator_id
    await node._handle_answer({"coordinator_id": None})

    # Should not change coordinator_id
    assert node.coordinator_id is None

    # Should not start election
    node._start_election.assert_not_called()


# =============================================================================
# Additional: _handle_answer ignores missing coordinator_id
# =============================================================================
@pytest.mark.asyncio
async def test_handle_answer_ignores_missing(node):
    """
    _handle_answer should ignore ANSWER without coordinator_id.
    """
    node._broadcast = AsyncMock()
    node._start_election = AsyncMock()

    # Receive ANSWER without coordinator_id
    await node._handle_answer({})

    # Should not change coordinator_id
    assert node.coordinator_id is None

    # Should not start election
    node._start_election.assert_not_called()


# =============================================================================
# Additional: _handle_answer with equal coordinator_id (self) - does nothing
# =============================================================================
@pytest.mark.asyncio
async def test_handle_answer_equal_id_does_nothing(node):
    """
    When ANSWER has coordinator_id == self.node_id, node does nothing
    (falls through without action). This prevents self-coordination via ANSWER.
    """
    node.coordinator_id = None  # Ensure not set
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._start_election = AsyncMock()

    # Receive ANSWER with same ID as self
    await node._handle_answer({"coordinator_id": 3})

    # coordinator_id should NOT be set (falls through)
    assert node.coordinator_id is None

    # Role should still be peer (default)
    assert node.role == "peer"

    # Should NOT start election (equal ID is not lower)
    node._start_election.assert_not_called()


# =============================================================================
# Additional: ANSWER accepted from much higher coordinator
# =============================================================================
@pytest.mark.asyncio
async def test_handle_answer_accepts_any_higher(node):
    """
    Node should accept any coordinator with higher ID than itself.
    """
    node.coordinator_id = None
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._start_election = AsyncMock()

    # Answer from ID 10 (way higher than 3)
    await node._handle_answer({"coordinator_id": 10})

    assert node.coordinator_id == 10
    assert node.role == "peer"
    node._start_election.assert_not_called()


# =============================================================================
# Additional: _wait_for_answer waits for coordinator_id to be set
# =============================================================================
@pytest.mark.asyncio
async def test_wait_for_answer_blocks_until_set(node):
    """
    _wait_for_answer should block until coordinator_id is set.
    """
    node.coordinator_id = None
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._start_election = AsyncMock()

    # Start waiting
    wait_task = asyncio.create_task(node._wait_for_answer())

    # Should not return immediately
    await asyncio.sleep(0.01)
    assert not wait_task.done()

    # Set coordinator_id
    await node._handle_answer({"coordinator_id": 9})

    # Now wait should complete
    await asyncio.wait_for(wait_task, timeout=1.0)

    assert node.coordinator_id == 9
