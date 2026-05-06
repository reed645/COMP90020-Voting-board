let ws = null;
let questions = [];
let state = {};
let lastMessageTime = 0;
let heartbeatTimer = null;

function connect() {
  ws = new WebSocket('ws://' + location.host + '/ws');

  ws.onopen = function() {
    console.log('connected');
    lastMessageTime = Date.now();
    setConnected(true);
    startHeartbeatCheck();
  };

  ws.onmessage = function(e) {
  lastMessageTime = Date.now();
  setConnected(true);
  var data = JSON.parse(e.data);

  if (data.type === 'status') {
    updateStatus(data);
  } else if (data.type === 'questions') {
    updateQuestions(data.questions);
  } else if (data.type === 'results') {
    updateResults(data.rankings);
  }
};

  ws.onclose = function() {
    console.log('disconnected, retrying...');
    setConnected(false);
    stopHeartbeatCheck();
    setTimeout(connect, 2000);
  };

  ws.onerror = function() {
    setConnected(false);
    ws.close();
  };
}

function startHeartbeatCheck() {
  stopHeartbeatCheck();
  heartbeatTimer = setInterval(function() {
    if (Date.now() - lastMessageTime > 3000) {
      setConnected(false);
    }
  }, 1000);
}

function stopHeartbeatCheck() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function setConnected(connected) {
  var statusEl = document.getElementById('status');
  if (connected) {
    statusEl.textContent = 'live';
    statusEl.className = 'status-live';
  } else {
    statusEl.textContent = 'DISCONNECTED';
    statusEl.className = 'status-disconnected';
  }
}

function updateStatus(data) {
  state = data;
  document.getElementById('node-id').textContent = data.node_id;
  document.getElementById('phase-name').textContent = 'PHASE : ' + data.phase.toUpperCase();
  var badge = document.getElementById('role-badge');
  badge.textContent = data.role;
  badge.className = data.role === 'coordinator' ? 'role-coordinator' : 'role-peer';
  document.getElementById('coordinator-info').textContent = 'Leader: Node ' + (data.coordinator_id || '—');

  var titles = { waiting: 'Ready', submission: 'Submit your question', voting: 'Vote on a question', closed: 'Session complete' };
  document.getElementById('phase-title').textContent = titles[data.phase] || data.phase;

  //Only show the current phase
  document.getElementById('phase-waiting').className = 'hidden';
  document.getElementById('phase-submission').className = 'hidden';
  document.getElementById('phase-voting').className = 'hidden';
  document.getElementById('phase-closed').className = 'hidden';

  if (data.phase === 'waiting') {
    document.getElementById('phase-waiting').className = '';
  } else if (data.phase === 'submission') {
    document.getElementById('phase-submission').className = '';
  } else if (data.phase === 'voting') {
    document.getElementById('phase-voting').className = '';
  } else if (data.phase === 'closed') {
    document.getElementById('phase-closed').className = '';
  }

  // user has already submitted a question
  if (data.has_submitted) {
    document.getElementById('question-input').className = 'hidden';
    document.getElementById('submit-btn').className = 'hidden';
    document.getElementById('submit-success').className = '';
  } else {
    document.getElementById('question-input').className = '';
    document.getElementById('submit-btn').className = '';
    document.getElementById('submit-success').className = 'hidden';
  }

  // user has already voted
  if (data.has_voted) {
    document.getElementById('voted-msg').className = '';
  } else {
    document.getElementById('voted-msg').className = 'hidden';
  }

  // countdown timer
  var timer = document.getElementById('timer');
  if (data.time_left !== null && data.time_left !== undefined) {
    var mins = Math.floor(data.time_left / 60);
    var secs = Math.floor(data.time_left % 60);
    timer.textContent = String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
  } else {
    timer.textContent = '00:00';
  }
}

function startSession() {
  fetch('/start', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) alert(data.error);
    });
}

function submitQuestion() {
  var input = document.getElementById('question-input');
  var text = input.value.trim();
  if (!text) return;

  fetch('/submit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question: text })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) alert(data.error);
      else input.value = '';
    });
}

function updateQuestions(qs) {
  questions = qs;
  var list = document.getElementById('vote-list');
  list.innerHTML = '';

  for (var i = 0; i < qs.length; i++) {
    var q = qs[i];
    var div = document.createElement('div');
    div.textContent = q.text + ' (' + q.votes + ' votes)';
    div.style.padding = '12px';
    div.style.border = '2px solid #0f3460';
    div.style.borderRadius = '8px';
    div.style.marginBottom = '8px';
    div.style.cursor = 'pointer';
    div.onclick = (function(id) {
      return function() {
        if (state.has_voted) return;
        castVote(id);
      };
    })(q.id);
    list.appendChild(div);
  }
}

function castVote(qid) {
  fetch('/vote', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question_ids: [qid] })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) alert(data.error);
    });
}

function updateResults(rankings) {
  var list = document.getElementById('results-list');
  list.innerHTML = '';

  if (!rankings || rankings.length === 0) {
    list.textContent = 'No results yet.';
    return;
  }

  for (var i = 0; i < rankings.length; i++) {
    var r = rankings[i];
    var div = document.createElement('div');
    div.textContent = '#' + (i + 1) + '  ' + r.text + ' — ' + r.votes + ' votes';
    div.style.padding = '12px';
    div.style.marginBottom = '8px';
    div.style.borderRadius = '5px';
    div.style.background = '#ecdccc';
    list.appendChild(div);
  }
}

connect();
