"""
    This is all the configuration for the node and state server. 
    Including Heartbeat setting for the election, state server address, and session durations.
"""
import os

# Heartbeat settings
HEARTBEAT_INTERVAL = 1.0  
TIMEOUT = 3.0  # heartbeat timeout before declaring coordinator dead
ELECTION_TIMEOUT = 2.0  # wait time for OK replies
QUERY_TIMEOUT = 2.0  # wait time for ANSWER replies

# State server address 
STATE_SERVER_URL = os.environ.get("STATE_SERVER_URL", "http://localhost:6000")
# Session durations
SUBMISSION_DURATION = 60  
VOTING_DURATION = 30 

# Phase 
PHASE_SUBMISSION = "submission"
PHASE_VOTING = "voting"
PHASE_CLOSED = "closed"
