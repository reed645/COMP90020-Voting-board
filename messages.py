"""
    This is the message type used for the communication between the nodes.
    Used Json for information transmission.

"""

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Message:
    # Basic message structure for all communications.
    #Transfer between message and Json
    type: str
    sender_id: int
    payload: dict = field(default_factory=dict)

    def to_json(self) -> str:
        """Convert message to JSON string."""
        return json.dumps({
            "type": self.type,
            "sender_id": self.sender_id,
            "payload": self.payload
        })

    @classmethod
    def from_json(cls, data: str) -> "Message":
        """Parse message from JSON."""
        parsed = json.loads(data)
        return cls(
            type=parsed["type"],
            sender_id=parsed["sender_id"],
            payload=parsed.get("payload", {})
        )


# Election-related message types, include Election, Ok, Query, Answer
@dataclass
class ElectionMessage(Message):
    def __init__(self, sender_id: int):
        super().__init__(type="ELECTION", sender_id=sender_id, payload={})


@dataclass
class OkMessage(Message):
    def __init__(self, sender_id: int):
        super().__init__(type="OK", sender_id=sender_id, payload={})


@dataclass
class QueryMessage(Message):
    def __init__(self, sender_id: int):
        super().__init__(type="QUERY", sender_id=sender_id, payload={})


@dataclass
class AnswerMessage(Message):
    def __init__(self, sender_id: int, coordinator_id: int):
        super().__init__(
            type="ANSWER",
            sender_id=sender_id,
            #Answer message containing coordinator ID 
            #so that the initiator can compare and find the coordinator
            payload={"coordinator_id": coordinator_id}
        )


@dataclass
class CoordinatorMessage(Message):
   #broadcast message announcing new coordinator
   #sender might not be the coordinator
    def __init__(self, sender_id: int, coordinator_id: int):
        super().__init__(
            type="COORDINATOR",
            sender_id=sender_id,
            payload={"coordinator_id": coordinator_id}
        )


@dataclass
class HeartbeatMessage(Message):
    def __init__(self, sender_id: int, coordinator_id: int, phase: str):
        super().__init__(
            type="HEARTBEAT",
            sender_id=sender_id,
            payload={"coordinator_id": coordinator_id, "phase": phase}
        )


# Session-related message types
@dataclass
class SubmitMessage(Message):
    #used to submit question during session
    def __init__(self, sender_id: int, question: str):
        super().__init__(
            type="SUBMIT",
            sender_id=sender_id,
            payload={"question": question}
        )


@dataclass
class VoteMessage(Message):
    #used to vote for question(s)
    def __init__(self, sender_id: int, question_ids: list):
        super().__init__(
            type="VOTE",
            sender_id=sender_id,
            payload={"question_ids": question_ids}
        )


@dataclass
class QuestionsMessage(Message):
    #used to broadcast questions
    def __init__(self, sender_id: int, questions: list):
        super().__init__(
            type="QUESTIONS",
            sender_id=sender_id,
            payload={"questions": questions}
        )


@dataclass
class ResultMessage(Message):
    #used to broadcast results/rankings
    def __init__(self, sender_id: int, rankings: list):
        super().__init__(
            type="RESULT",
            sender_id=sender_id,
            payload={"rankings": rankings}
        )




@dataclass
class SessionUpdate(Message):
   
    def __init__(self, sender_id: int, phase: str):
        super().__init__(
            type="SESSION_UPDATE",
            sender_id=sender_id,
            payload={"phase": phase}

        
        )



@dataclass
class SessionResume(Message):

    def __init__(self, sender_id: int, phase: str, questions: list = None, votes: dict = None):
        super().__init__(
            type="SESSION_RESUME",
            sender_id=sender_id,
            payload={
                "phase": phase,
                "questions": questions or [],
                "votes": votes or {}
            }
        )

#trace event for debugging
@dataclass
class TraceMessage(Message):
    def __init__(self, sender_id: int, event: str, detail: dict = None):
        super().__init__(
            type="TRACE",
            sender_id=sender_id,
            payload={"event": event, "detail": detail or {}}
        )

@dataclass
class StepDownAck(Message):
    def __init__(self, sender_id: int):
        super().__init__(type = "STEP_DOWN_ACK", sender_id = sender_id, payload = {})


#create message according to the message type
def create_message(msg_type: str, sender_id: int, payload: dict = None) -> Message:
    
    payload = payload or {}

    message_classes = {
        "ELECTION": ElectionMessage,
        "OK": OkMessage,
        "QUERY": QueryMessage,
        "ANSWER": AnswerMessage,
        "COORDINATOR": CoordinatorMessage,
        "HEARTBEAT": HeartbeatMessage,
        "SUBMIT": SubmitMessage,
        "VOTE": VoteMessage,
        "QUESTIONS": QuestionsMessage,
        "RESULT": ResultMessage,
        "SESSION_UPDATE": SessionUpdate,
        "SESSION_RESUME": SessionResume,
        "TRACE": TraceMessage,
        "STEP_DOWN_ACK": StepDownAck
    }


    if msg_type == "HEARTBEAT":
        return HeartbeatMessage(
            sender_id,
            payload.get("coordinator_id"),
            payload.get("phase", "unknown")
        )

    elif msg_type == "SUBMIT":
        return SubmitMessage(sender_id, payload.get("question", ""))
    elif msg_type == "VOTE":
        return VoteMessage(sender_id, payload.get("question_ids", []))
    elif msg_type == "QUESTIONS":
        return QuestionsMessage(sender_id, payload.get("questions", []))
    elif msg_type == "RESULT":
        return ResultMessage(sender_id, payload.get("rankings", []))
    elif msg_type == "SESSION_UPDATE":
        return SessionUpdate(sender_id, payload.get("phase", "unknown"))
    elif msg_type == "SESSION_RESUME":
        return SessionResume(
            sender_id,
            payload.get("phase", "unknown"),
            payload.get("questions", []),
            payload.get("votes", {})
        )

    elif msg_type == "ANSWER":
        return AnswerMessage(sender_id, payload.get("coordinator_id", 0))
    elif msg_type == "COORDINATOR":
        return CoordinatorMessage(sender_id, payload.get("coordinator_id", 0))
    elif msg_type == "TRACE":
        return TraceMessage(sender_id, payload.get("event", ""), payload.get("detail", {}))
    elif msg_type in message_classes:
        return message_classes[msg_type](sender_id)

    return Message(type=msg_type, sender_id=sender_id, payload=payload)
