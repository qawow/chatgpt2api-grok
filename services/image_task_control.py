"""One monotonic deadline shared by image polling, downloads and failover."""
from __future__ import annotations

import threading
import time


class ImageTaskControl:
    def __init__(self, timeout: float):
        self.deadline = time.monotonic() + max(0.001, timeout)
        self.cancelled = threading.Event()

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set() or time.monotonic() >= self.deadline

    def remaining(self, maximum: float | None = None) -> float:
        seconds = self.deadline - time.monotonic()
        if self.cancelled.is_set() or seconds <= 0:
            raise TimeoutError("图片任务超时或执行已取消")
        return seconds if maximum is None else min(seconds, maximum)

    def wait(self, seconds: float) -> None:
        self.cancelled.wait(self.remaining(max(0.0, seconds)))
        self.remaining()
