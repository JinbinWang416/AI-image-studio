# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import unittest

from app.orchestrator import RateLimiter


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_are_paced_and_cooldown_delays_all_workers(self) -> None:
        # 600 RPM gives a 100 ms interval, keeping this timing test short while
        # still proving that the limiter does not release an initial burst.
        limiter = RateLimiter(600)
        await limiter.acquire()

        started = time.monotonic()
        await limiter.acquire()
        self.assertGreaterEqual(time.monotonic() - started, 0.08)

        await limiter.cooldown(0.10)
        started = time.monotonic()
        await limiter.acquire()
        self.assertGreaterEqual(time.monotonic() - started, 0.08)


if __name__ == "__main__":
    unittest.main()
