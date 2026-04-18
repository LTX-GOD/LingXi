from __future__ import annotations

from dataclasses import dataclass
import threading


def normalize_host_url(value: str, *, default_scheme: str = "http") -> str:
    normalized = str(value or "").strip().rstrip("/")
    if normalized and not normalized.startswith(("http://", "https://")):
        normalized = f"{default_scheme}://{normalized}"
    return normalized.rstrip("/")


def is_failover_worthy_http_response(status_code: int, content_type: str = "") -> bool:
    if status_code in {404, 408}:
        return True
    if status_code >= 500:
        return True
    normalized_type = str(content_type or "").lower()
    if status_code == 200 and normalized_type and "json" not in normalized_type:
        return True
    return False


@dataclass(frozen=True)
class HostFailoverSnapshot:
    primary: str
    fallback: str
    active: str
    failure_streak: int
    switched: bool
    threshold: int


class HostFailoverState:
    def __init__(
        self,
        primary: str = "",
        fallback: str = "",
        *,
        threshold: int = 2,
        default_scheme: str = "http",
    ) -> None:
        self.primary = normalize_host_url(primary, default_scheme=default_scheme)
        normalized_fallback = normalize_host_url(
            fallback,
            default_scheme=default_scheme,
        )
        self.fallback = normalized_fallback if normalized_fallback != self.primary else ""
        self.threshold = max(1, int(threshold or 2))
        self.active = self.primary
        self.failure_streak = 0
        self._lock = threading.Lock()

    def _snapshot_unlocked(self) -> HostFailoverSnapshot:
        return HostFailoverSnapshot(
            primary=self.primary,
            fallback=self.fallback,
            active=self.active,
            failure_streak=self.failure_streak,
            switched=bool(self.fallback and self.active == self.fallback),
            threshold=self.threshold,
        )

    def snapshot(self) -> HostFailoverSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def record_success(self, host: str) -> HostFailoverSnapshot:
        normalized_host = normalize_host_url(host)
        with self._lock:
            if normalized_host == self.primary:
                self.failure_streak = 0
            if normalized_host:
                self.active = normalized_host
            return self._snapshot_unlocked()

    def record_failure(self, host: str) -> tuple[HostFailoverSnapshot, bool]:
        normalized_host = normalize_host_url(host)
        switched = False
        with self._lock:
            if normalized_host != self.primary or not self.fallback:
                return self._snapshot_unlocked(), False
            self.failure_streak += 1
            if self.failure_streak >= self.threshold and self.active != self.fallback:
                self.active = self.fallback
                switched = True
            return self._snapshot_unlocked(), switched
