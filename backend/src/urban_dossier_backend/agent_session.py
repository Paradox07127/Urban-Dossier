"""In-memory session store for agent mode. Caches analysis payloads so LLM can iterate without re-running deterministic analysis."""

import time
import uuid
import threading

MAX_SESSIONS = 50
MAX_CHAT_HISTORY = 20  # keep last 10 exchanges
MAX_STORED_REPORTS = 3
SESSION_TTL = 1800  # 30 minutes


class AgentSession:
    def __init__(self, session_id: str, analysis_payload: dict):
        self.session_id = session_id
        self.analysis_payload = analysis_payload
        self.chat_history: list[dict] = []  # [{role: "user"|"assistant", content: str}]
        self.generated_reports: list[str] = []  # HTML strings
        self.created_at = time.time()
        self.last_accessed = time.time()

    def touch(self):
        self.last_accessed = time.time()

    def add_chat(self, role: str, content: str):
        self.chat_history.append({"role": role, "content": content})
        # Cap stored history to prevent memory growth
        if len(self.chat_history) > MAX_CHAT_HISTORY:
            self.chat_history = self.chat_history[-MAX_CHAT_HISTORY:]

    def add_report(self, html: str):
        self.generated_reports.append(html)
        if len(self.generated_reports) > MAX_STORED_REPORTS:
            self.generated_reports.pop(0)


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, AgentSession] = {}
        self._lock = threading.Lock()

    def create(self, payload: dict) -> str:
        sid = uuid.uuid4().hex  # full 128-bit entropy
        with self._lock:
            self._cleanup()
            self._sessions[sid] = AgentSession(sid, payload)
        return sid

    def get(self, session_id: str) -> AgentSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.touch()
            return session

    def _cleanup(self):
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v.last_accessed > SESSION_TTL]
        for k in expired:
            del self._sessions[k]
        # Evict oldest if over limit
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].last_accessed)
            del self._sessions[oldest]


store = SessionStore()
