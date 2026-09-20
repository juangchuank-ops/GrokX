"""Sidecar Token 缓冲池测试（不依赖 Playwright，用假 Worker 驱动）。"""

from __future__ import annotations

import threading
import time
import unittest

from sidecar.browser_worker import (
    InteractiveChallengeError,
    SidecarError,
    TurnstileChallengeError,
    playwright_proxy_kwargs,
)
from sidecar.token_pool import CachedToken, TokenPool


class _FakeWorker:
    def __init__(self, *, delay: float = 0.0, fail_times: int = 0, fail_with: type[Exception] = TurnstileChallengeError):
        self.delay = delay
        self.fail_times = fail_times
        self.fail_with = fail_with
        self.produced = 0
        self.tokens_produced = 0
        self.restarts = 0
        self.castle_calls = 0
        self.lock = threading.Lock()

    def produce_turnstile_token(self, action: str = "", timeout: float = 25.0) -> str:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise self.fail_with("boom")
            self.produced += 1
            self.tokens_produced += 1
            return f"ts-{self.produced}"

    def produce_castle_token(self, timeout: float = 20.0) -> str:
        with self.lock:
            self.castle_calls += 1
            if self.castle_calls == 1 and getattr(self, "castle_fail_first", False):
                raise SidecarError("castle transient failure")
            return f"castle-{self.castle_calls}"

    def restart_context(self) -> None:
        self.restarts += 1


class ProxyNormalizationTest(unittest.TestCase):
    def test_socks5h_is_downgraded_for_chromium(self):
        self.assertEqual(
            {"server": "socks5://1.2.3.4:1080"},
            playwright_proxy_kwargs("socks5h://1.2.3.4:1080"),
        )

    def test_auth_is_split_into_fields(self):
        kwargs = playwright_proxy_kwargs("http://user:p%40ss@proxy.local:8080")
        self.assertEqual("http://proxy.local:8080", kwargs["server"])
        self.assertEqual("user", kwargs["username"])
        self.assertEqual("p@ss", kwargs["password"])

    def test_empty_proxy_returns_no_kwargs(self):
        self.assertEqual({}, playwright_proxy_kwargs(""))

    def test_unsupported_scheme_raises(self):
        with self.assertRaises(ValueError):
            playwright_proxy_kwargs("ftp://proxy.local:21")


class TokenPoolTest(unittest.TestCase):
    def _pool(self, worker, **kwargs) -> TokenPool:
        defaults = {"pool_size": 2, "produce_timeout": 2.0, "restart_after": 1000}
        defaults.update(kwargs)
        return TokenPool(worker, **defaults)

    def test_refills_and_serves_from_buffer(self):
        worker = _FakeWorker()
        pool = self._pool(worker)
        pool.start()
        try:
            deadline = time.time() + 3
            while pool.stats()["queued"] < 2 and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(pool.stats()["queued"], 1)
            token = pool.get_turnstile_token(timeout=2)
            self.assertTrue(token.startswith("ts-"))
            self.assertEqual(1, pool.stats()["served"])
        finally:
            pool.stop()

    def test_expired_tokens_are_purged(self):
        worker = _FakeWorker()
        pool = self._pool(worker, pool_size=1, max_age_sec=30)
        pool._queue.append(CachedToken("stale", time.time() - 999))
        pool._purge_expired()
        self.assertEqual([], pool._queue)
        self.assertEqual(1, pool.stats()["expired"])

    def test_serves_fresh_token_on_starvation(self):
        worker = _FakeWorker()
        pool = self._pool(worker, pool_size=1)
        # 不启动补水线程，直接消费 -> 应降级为实时生成
        token = pool.get_turnstile_token(timeout=0.2)
        self.assertTrue(token.startswith("ts-"))

    def test_consecutive_failures_report_proxy_unhealthy(self):
        events: list[str] = []
        worker = _FakeWorker(fail_times=10, fail_with=InteractiveChallengeError)
        pool = self._pool(
            worker,
            pool_size=1,
            unhealthy_threshold=3,
            on_event=lambda event, _payload: events.append(event),
        )
        pool.start()
        try:
            deadline = time.time() + 5
            while "proxy_unhealthy" not in events and time.time() < deadline:
                time.sleep(0.05)
        finally:
            pool.stop()
        self.assertIn("proxy_unhealthy", events)
        self.assertGreaterEqual(worker.restarts, 1)
        self.assertTrue(pool.stats()["proxy_unhealthy"])

    def test_soft_restart_after_token_budget(self):
        worker = _FakeWorker()
        pool = self._pool(worker, pool_size=1, restart_after=1)
        pool.start()
        try:
            deadline = time.time() + 5
            while worker.restarts < 1 and time.time() < deadline:
                time.sleep(0.05)
        finally:
            pool.stop()
        self.assertGreaterEqual(worker.restarts, 1)

    def test_castle_token_is_fetched_live(self):
        worker = _FakeWorker()
        pool = self._pool(worker)
        self.assertEqual("castle-1", pool.get_castle_token())

    def test_castle_token_retries_then_raises(self):
        worker = _FakeWorker()
        worker.castle_fail_first = True
        pool = self._pool(worker)
        self.assertEqual("castle-2", pool.get_castle_token(attempts=2))

        worker2 = _FakeWorker()
        worker2.castle_fail_first = True
        pool2 = self._pool(worker2)
        with self.assertRaises(SidecarError):
            pool2.get_castle_token(attempts=1)


if __name__ == "__main__":
    unittest.main()
