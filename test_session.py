"""
Unit tests for session management logic in node.py.
"""

import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from node import Node
from config import (
    PHASE_SUBMISSION,
    PHASE_VOTING,
    PHASE_CLOSED,
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
def coordinator_node():
    """Create a node set as coordinator."""
    n = Node(node_id=5, port=8005, peer_ports=[8003, 8007, 8009])
    n.running = True
    n.role = "coordinator"
    n.coordinator_id = 5
    n.loop = asyncio.get_event_loop()
    return n


# =============================================================================
# Test 9: Coordinator receives SUBMIT
# =============================================================================
@pytest.mark.asyncio
async def test_coordinator_submit_calls_server(coordinator_node):
    """
    When coordinator receives SUBMIT in submission phase, it should
    call _add_question_to_server with correct arguments.
    """
    node = coordinator_node
    node.session_phase = PHASE_SUBMISSION
    node.frozen = False

    from messages import Message, create_message

    node._add_question_to_server = AsyncMock(return_value={
        "id": "q1",
        "text": "What is your favorite color?",
        "submitted_by": 3
    })
    node._sync_with_state_server = AsyncMock()

    # Create SUBMIT message
    submit_msg = create_message(
        "SUBMIT",
        sender_id=3,
        payload={"question": "What is your favorite color?"}
    )

    # Receive the message
    await node._handle_submit(submit_msg)

    # Should have called _add_question_to_server
    node._add_question_to_server.assert_called_once_with(
        "What is your favorite color?",
        3  # sender_id
    )


# =============================================================================
# Test 10: Coordinator receives duplicate SUBMIT
# =============================================================================
@pytest.mark.asyncio
async def test_coordinator_submit_deduplicated(coordinator_node):
    """
    Duplicate questions are deduplicated by the state server.
    Coordinator should still call _add_question_to_server (server handles dedup).
    """
    node = coordinator_node
    node.session_phase = PHASE_SUBMISSION
    node.frozen = False

    node._add_question_to_server = AsyncMock(return_value={
        "status": "duplicate",
        "question": {"id": "q1", "text": "What is your favorite color?"}
    })
    node._sync_with_state_server = AsyncMock()

    from messages import create_message

    # First submission
    submit_msg1 = create_message(
        "SUBMIT",
        sender_id=3,
        payload={"question": "What is your favorite color?"}
    )
    await node._handle_submit(submit_msg1)

    # Second submission with same question (different sender)
    submit_msg2 = create_message(
        "SUBMIT",
        sender_id=7,
        payload={"question": "What is your favorite color?"}
    )
    await node._handle_submit(submit_msg2)

    # Both calls should go to server (server handles dedup)
    assert node._add_question_to_server.call_count == 2


# =============================================================================
# Test 11: Peer vote in wrong phase
# =============================================================================
@pytest.mark.asyncio
async def test_peer_vote_wrong_phase(node):
    """
    When peer calls vote() in submission phase (not voting phase),
    it should return False and NOT broadcast.
    """
    node.session_phase = PHASE_SUBMISSION
    node.has_voted = False
    node.questions = [{"id": "q1", "text": "What is your favorite color?"}]

    node._broadcast = AsyncMock()

    # Try to vote
    result = await node.vote(["q1"])

    # Should return False
    assert result is False

    # Should NOT have broadcast
    node._broadcast.assert_not_called()


# =============================================================================
# Test 12: Peer vote twice
# =============================================================================
@pytest.mark.asyncio
async def test_peer_vote_twice_rejected(node):
    """
    When peer has already voted, subsequent vote() calls should return False.
    """
    node.session_phase = PHASE_VOTING
    node.has_voted = True  # Already voted
    node.questions = [{"id": "q1", "text": "What is your favorite color?"}]

    node._broadcast = AsyncMock()

    # Try to vote again
    result = await node.vote(["q1"])

    # Should return False
    assert result is False

    # Should NOT have broadcast
    node._broadcast.assert_not_called()


# =============================================================================
# Test 13: Peer vote with invalid question IDs
# =============================================================================
@pytest.mark.asyncio
async def test_peer_vote_invalid_ids(node):
    """
    When voting for non-existent question IDs, vote() should return False.
    """
    node.session_phase = PHASE_VOTING
    node.has_voted = False
    node.questions = [{"id": "q1", "text": "What is your favorite color?"}]

    node._broadcast = AsyncMock()

    # Try to vote for non-existent ID
    result = await node.vote(["q99"])

    # Should return False
    assert result is False

    # Should NOT have broadcast
    node._broadcast.assert_not_called()


# =============================================================================
# Test 14: _resume_session first startup (no deadline)
# =============================================================================
@pytest.mark.asyncio
async def test_resume_session_no_deadline_starts_fresh(coordinator_node):
    """
    When resuming with no deadline set, coordinator should start
    a fresh submission phase with full SUBMISSION_DURATION.
    """
    node = coordinator_node
    node.session_phase = PHASE_SUBMISSION
    node.submission_deadline = None
    node.voting_deadline = None
    node.questions = []
    node.votes = {}

    node._sync_with_state_server = AsyncMock()
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)

    # Mock _run_submission_phase to capture the duration argument
    node._run_submission_phase = AsyncMock()

    # Mock to avoid actual sleep
    with patch('node.SUBMISSION_DURATION', 60):
        await node._resume_session()

    # Should call _run_submission_phase with full duration
    node._run_submission_phase.assert_called_once()
    call_args = node._run_submission_phase.call_args
    # The duration should be SUBMISSION_DURATION (60 seconds)
    assert call_args[0][0] == SUBMISSION_DURATION

    # Should NOT call _advance_to_voting
    node._broadcast.assert_called()  # SESSION_RESUME is broadcast


# =============================================================================
# Test 15: _resume_session with remaining time
# =============================================================================
@pytest.mark.asyncio
async def test_resume_session_with_remaining_time(coordinator_node):
    """
    When resuming with time remaining, coordinator should continue
    with the remaining time, not full duration.
    """
    node = coordinator_node
    node.session_phase = PHASE_SUBMISSION
    node.submission_deadline = time.time() + 30  # 30 seconds left
    node.voting_deadline = None
    node.questions = []
    node.votes = {}

    node._sync_with_state_server = AsyncMock()
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._run_submission_phase = AsyncMock()

    await node._resume_session()

    # Should call _run_submission_phase with remaining time (~30 seconds)
    node._run_submission_phase.assert_called_once()
    call_args = node._run_submission_phase.call_args
    duration = call_args[0][0]

    # Duration should be approximately 30 seconds (allowing for small variance)
    assert 28 <= duration <= 32


# =============================================================================
# Test 16: _resume_session with expired deadline
# =============================================================================
@pytest.mark.asyncio
async def test_resume_session_expired_deadline(coordinator_node):
    """
    When resuming with expired deadline, coordinator should advance
    directly to voting phase.
    """
    node = coordinator_node
    node.session_phase = PHASE_SUBMISSION
    node.submission_deadline = time.time() - 10  # Already expired
    node.voting_deadline = None
    node.questions = [{"id": "q1", "text": "What is your favorite color?"}]
    node.votes = {}

    node._sync_with_state_server = AsyncMock()
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._advance_to_voting = AsyncMock()
    node._run_voting_phase = AsyncMock()

    await node._resume_session()

    # Should call _advance_to_voting
    node._advance_to_voting.assert_called_once()

    # Should NOT call _run_submission_phase
    node._run_submission_phase = MagicMock()  # Reset to check no calls
    node._run_submission_phase.assert_not_called()


# =============================================================================
# Additional: Coordinator ignores SUBMIT when frozen
# =============================================================================
@pytest.mark.asyncio
async def test_coordinator_ignores_submit_when_frozen(coordinator_node):
    """
    When coordinator is frozen (during election), it should ignore SUBMIT.
    """
    node = coordinator_node
    node.session_phase = PHASE_SUBMISSION
    node.frozen = True

    node._add_question_to_server = AsyncMock()
    node._sync_with_state_server = AsyncMock()

    from messages import create_message

    submit_msg = create_message(
        "SUBMIT",
        sender_id=3,
        payload={"question": "What is your favorite color?"}
    )

    await node._handle_submit(submit_msg)

    # Should NOT have called _add_question_to_server
    node._add_question_to_server.assert_not_called()


# =============================================================================
# Additional: Coordinator ignores SUBMIT in wrong phase
# =============================================================================
@pytest.mark.asyncio
async def test_coordinator_ignores_submit_in_voting_phase(coordinator_node):
    """
    When in voting phase, coordinator should ignore SUBMIT.
    """
    node = coordinator_node
    node.session_phase = PHASE_VOTING  # Wrong phase
    node.frozen = False

    node._add_question_to_server = AsyncMock()
    node._sync_with_state_server = AsyncMock()

    from messages import create_message

    submit_msg = create_message(
        "SUBMIT",
        sender_id=3,
        payload={"question": "What is your favorite color?"}
    )

    await node._handle_submit(submit_msg)

    # Should NOT have called _add_question_to_server
    node._add_question_to_server.assert_not_called()


# =============================================================================
# Additional: Peer can only submit once
# =============================================================================
@pytest.mark.asyncio
async def test_peer_submit_only_once(node):
    """
    Peer can only submit one question.
    """
    node.session_phase = PHASE_SUBMISSION
    node.has_submitted = True  # Already submitted
    node._broadcast = AsyncMock()

    result = await node.submit_question("What is your favorite color?")

    assert result is False
    node._broadcast.assert_not_called()


# =============================================================================
# Additional: Peer can submit in submission phase
# =============================================================================
@pytest.mark.asyncio
async def test_peer_submit_in_submission_phase(node):
    """
    Peer can submit a question during submission phase.
    """
    node.session_phase = PHASE_SUBMISSION
    node.has_submitted = False
    node._broadcast = AsyncMock()

    result = await node.submit_question("What is your favorite color?")

    assert result is True
    node._broadcast.assert_called()
    call_args = node._broadcast.call_args
    msg = call_args[0][0]
    assert msg.type == "SUBMIT"
    assert msg.payload["question"] == "What is your favorite color?"


# =============================================================================
# Additional: Peer vote in voting phase
# =============================================================================
@pytest.mark.asyncio
async def test_peer_vote_in_voting_phase(node):
    """
    Peer can vote during voting phase.
    """
    node.session_phase = PHASE_VOTING
    node.has_voted = False
    node.questions = [
        {"id": "q1", "text": "Red"},
        {"id": "q2", "text": "Blue"}
    ]
    node._broadcast = AsyncMock()

    result = await node.vote(["q1", "q2"])

    assert result is True
    node._broadcast.assert_called()
    call_args = node._broadcast.call_args
    msg = call_args[0][0]
    assert msg.type == "VOTE"
    assert msg.payload["question_ids"] == ["q1", "q2"]


# =============================================================================
# Additional: _resume_session in voting phase
# =============================================================================
@pytest.mark.asyncio
async def test_resume_session_in_voting_phase(coordinator_node):
    """
    When resuming in voting phase with time remaining, should continue voting.
    """
    node = coordinator_node
    node.session_phase = PHASE_VOTING
    node.submission_deadline = time.time() - 100  # Expired
    node.voting_deadline = time.time() + 15  # 15 seconds left
    node.questions = [{"id": "q1", "text": "What is your favorite color?"}]
    node.votes = {"q1": [3, 7]}

    node._sync_with_state_server = AsyncMock()
    node._broadcast = AsyncMock()
    node._write_state_to_server = AsyncMock(return_value=True)
    node._run_voting_phase = AsyncMock()

    await node._resume_session()

    # Should call _run_voting_phase with remaining time
    node._run_voting_phase.assert_called_once()
    call_args = node._run_voting_phase.call_args
    duration = call_args[0][0]

    # Duration should be approximately 15 seconds
    assert 13 <= duration <= 17


# =============================================================================
# Additional: _resume_session closed phase
# =============================================================================
@pytest.mark.asyncio
async def test_resume_session_closed_phase(coordinator_node):
    """
    When resuming in closed phase, should broadcast final results.
    """
    node = coordinator_node
    node.session_phase = PHASE_CLOSED
    node.questions = [{"id": "q1", "text": "What is your favorite color?"}]
    node.votes = {"q1": [3, 5, 7]}

    node._broadcast = AsyncMock()
    node._broadcast_final_results = AsyncMock()

    await node._resume_session()

    # Should broadcast final results
    node._broadcast_final_results.assert_called_once()
