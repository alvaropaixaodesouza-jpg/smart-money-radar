"""Tiny sliding-window rate limiter shared by API clients."""
from __future__ import annotations

import logging
import time
from collections import deque

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, max_per_min: int, name: str = "api"):
        self.max = max_per_min
        self.name = name
        self.calls: deque[float] = deque()
        self.total = 0

    def wait(self) -> None:
        now = time.monotonic()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= self.max:
            sleep = 60 - (now - self.calls[0]) + 0.05
            log.info("%s rate limit: sleeping %.1fs", self.name, sleep)
            time.sleep(max(sleep, 0))
        self.calls.append(time.monotonic())
        self.total += 1
