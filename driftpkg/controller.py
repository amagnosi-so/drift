from __future__ import annotations

import threading


class AdaptiveController:
    def __init__(
        self,
        base_workers: int,
        max_workers: int,
        base_rate: int,
        max_rate: int,
    ):
        self._lock = threading.Lock()
        self._errors = 0
        self._success = 0
        self._rate = base_rate
        self._workers = base_workers
        self._max_workers = max(1, max_workers)
        self._max_rate = max_rate

    def record_success(self):
        with self._lock:
            self._success += 1
            if self._success % 10 == 0 and self._errors < 3:
                self._rate = min(int(self._rate * 1.2), self._max_rate)
                self._workers = min(self._workers + 1, self._max_workers)

    def record_error(self):
        with self._lock:
            self._errors += 1
            self._rate = max(int(self._rate * 0.5), 200 * 1024)
            self._workers = max(1, self._workers - 1)

    def get_rate(self) -> int:
        with self._lock:
            return self._rate

    def get_workers(self) -> int:
        with self._lock:
            return self._workers
