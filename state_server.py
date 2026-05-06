"""
A fixed State server for the distributed voting application.
A Flask HTTP server running on port 6000 that stores the full session state
in memory and persists to state.json using atomic writes.
"""

import json
import os
import threading
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

STATE_FILE = "state.json"
state_lock = threading.Lock()
current_state = None


def load_state() -> dict:
    #load state from file or return default state.
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return get_default_state()


def get_default_state() -> dict:
    #return default empty state.
    return {
        "coordinator_id": None,
        "phase": "waiting",
        "questions": [],
        "votes": {},
        "submission_deadline": None,
        "voting_deadline": None
    }


def save_state(state: dict) -> None:
    #save state to file atomically (write to temp file, then replace).
    temp_file = "state.tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_file, STATE_FILE)


@app.route("/state", methods=["GET"])
def get_state():
    #return current state as JSON.
    with state_lock:
        if current_state is None:
            state = load_state()
        else:
            state = current_state
    return jsonify(state)


@app.route("/state", methods=["POST"])
def set_state():
    global current_state
    #replace current state with request body.
    new_state = request.get_json()
    if new_state is None:
        return jsonify({"error": "Invalid JSON"}), 400

    with state_lock:
        current_state = new_state
        save_state(new_state)

    return jsonify({"status": "ok"})


@app.route("/state/votes", methods=["POST"])
def add_vote():
    #append a vote, deduplicating by sender_id.
    data = request.get_json()
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    question_id = data.get("question_id")
    sender_id = data.get("sender_id")

    if not question_id or sender_id is None:
        return jsonify({"error": "Missing question_id or sender_id"}), 400

    with state_lock:
        global current_state
        if current_state is None:
            state = load_state()
        else:
            state = current_state

        # Initialize votes list for question if needed
        if question_id not in state["votes"]:
            state["votes"][question_id] = []

        # Deduplicate by sender_id
        if sender_id not in state["votes"][question_id]:
            state["votes"][question_id].append(sender_id)

        current_state = state
        save_state(state)

    return jsonify({"status": "ok", "votes": state["votes"].get(question_id, [])})


@app.route("/state/questions", methods=["POST"])
def add_question():
    #add a question, deduplicating by content.
    data = request.get_json()
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    question_text = data.get("question")
    submitted_by = data.get("submitted_by")

    if not question_text or submitted_by is None:
        return jsonify({"error": "Missing question or submitted_by"}), 400

    with state_lock:
        global current_state
        if current_state is None:
            state = load_state()
        else:
            state = current_state

        # Check if question already exists (deduplication by content)
        for q in state["questions"]:
            if q["text"] == question_text:
                return jsonify({
                    "status": "duplicate",
                    "question": q,
                    "message": "Question already exists"
                })

        # Generate new question ID
        existing_ids = [int(q["id"][1:]) for q in state["questions"] if q["id"].startswith("q") and q["id"][1:].isdigit()]
        new_id_num = max(existing_ids, default=0) + 1
        new_id = f"q{new_id_num}"

        new_question = {
            "id": new_id,
            "text": question_text,
            "submitted_by": submitted_by
        }
        state["questions"].append(new_question)

        current_state = state
        save_state(state)

    return jsonify({"status": "ok", "question": new_question})


@app.route("/state/phase", methods=["POST"])
def update_phase():
    #update the current phase and optionally deadlines.
    data = request.get_json()
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    with state_lock:
        global current_state
        if current_state is None:
            state = load_state()
        else:
            state = current_state

        if "phase" in data:
            state["phase"] = data["phase"]
        if "submission_deadline" in data:
            state["submission_deadline"] = data["submission_deadline"]
        if "voting_deadline" in data:
            state["voting_deadline"] = data["voting_deadline"]

        current_state = state
        save_state(state)

    return jsonify({"status": "ok", "phase": state["phase"]})


@app.route("/state/coordinator", methods=["POST"])
def update_coordinator():
    global current_state
    data = request.get_json()
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    with state_lock:
        if current_state is None:
            state = load_state()
        else:
            state = current_state
        state["coordinator_id"] = data.get("coordinator_id")
        current_state = state
        save_state(state)

    return jsonify({"status": "ok"})


@app.route("/reset", methods=["POST"])
def reset_state():
    global current_state
    #reset state to default (for testing).
    with state_lock:
        global current_state
        current_state = get_default_state()
        save_state(current_state)
    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    #health check endpoint.
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


def run_server(host: str = "0.0.0.0", port: int = 6000):
    #run the Flask server.
    # initialize state on startup
    global current_state
    with state_lock:
        current_state = load_state()

    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    run_server()
