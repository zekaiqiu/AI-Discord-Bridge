"""Shared test helpers."""

from __future__ import annotations

from datetime import datetime, timedelta


class FakeClock:
    """Injectable clock callable for deterministic time-based tests.

    Tests advance simulated time via .advance(seconds); no real sleeping.
    """

    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)
