"""Localhost hardening: one-time launch token -> session cookie, Host/Origin allow-lists."""
import secrets
import threading

COOKIE_NAME = "adstudio_session"


class SessionAuth:
    def __init__(self, allowed_hosts: set[str] | None = None):
        self.session_token = secrets.token_urlsafe(32)
        self._one_time: set[str] = set()
        self._lock = threading.Lock()
        self.allowed_hosts = allowed_hosts if allowed_hosts is not None else {"127.0.0.1", "localhost", "[::1]"}
        # Server mode only: a user-supplied secret that works as a reusable launch token (set by main.serve).
        self.access_token: str | None = None

    def issue_one_time(self) -> str:
        t = secrets.token_urlsafe(24)
        with self._lock:
            self._one_time.add(t)
        return t

    def consume_one_time(self, token: str) -> bool:
        if self.access_token and secrets.compare_digest(token.encode(), self.access_token.encode()):
            return True  # server mode: the configured access token can sign in any number of times
        with self._lock:
            if token in self._one_time:
                self._one_time.discard(token)
                return True
        return False

    def valid_session(self, cookie: str | None) -> bool:
        return bool(cookie) and secrets.compare_digest(cookie, self.session_token)

    @staticmethod
    def _hostname(host_header: str) -> str:
        host = host_header.strip().lower()
        if host.startswith("["):  # IPv6 literal
            return host.split("]")[0] + "]"
        return host.rsplit(":", 1)[0] if ":" in host else host

    def host_ok(self, host_header: str | None) -> bool:
        return bool(host_header) and self._hostname(host_header) in self.allowed_hosts

    def origin_ok(self, origin: str | None) -> bool:
        """Browsers send Origin on cross-site and non-GET requests; absent is fine (same-origin GET / curl)."""
        if not origin:
            return True
        if origin == "null":
            return False
        rest = origin.split("://", 1)[-1]
        return self._hostname(rest) in self.allowed_hosts
