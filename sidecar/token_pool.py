#!/usr/bin/env python3
"""sidecar/token_pool.py: 线程安全的 Token 缓冲池与生命周期管理。

- **Turnstile**：FIFO 缓冲 + TTL 淘汰 + 自适应补水，业务侧出队延迟为 0ms。
- **Castle**：按需实时从浏览器上下文提取（本身开销低，且强绑定当前设备会话）。
- **自愈**：连续失败达到阈值上报 ``proxy_unhealthy``；累计产出达到阈值触发 Context 软重启。
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Optional

from sidecar.browser_worker import (
    BrowserWorker,
    InteractiveChallengeError,
    SidecarError,
    TurnstileChallengeError,
)

# Turnstile Token 默认生命周期 300s，留 60s 安全冗余
DEFAULT_MAX_AGE_SEC = 240.0
DEFAULT_UNHEALTHY_THRESHOLD = 3
DEFAULT_RESTART_AFTER = 100


@dataclass(frozen=True)
class CachedToken:
    value: str
    created_at: float
    source: str = "turnstile"

    def age(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.created_at


class TokenPool:
    def __init__(
        self,
        worker: BrowserWorker,
        *,
        pool_size: int = 2,
        max_age_sec: float = DEFAULT_MAX_AGE_SEC,
        action: str = "",
        produce_timeout: float = 25.0,
        unhealthy_threshold: int = DEFAULT_UNHEALTHY_THRESHOLD,
        restart_after: int = DEFAULT_RESTART_AFTER,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ):
        self.worker = worker
        self.pool_size = max(1, int(pool_size))
        self.max_age_sec = max(30.0, float(max_age_sec))
        self.action = str(action or "")
        self.produce_timeout = max(1.0, float(produce_timeout))
        self.unhealthy_threshold = max(1, int(unhealthy_threshold))
        self.restart_after = max(1, int(restart_after))
        self.on_event = on_event or (lambda _event, _payload: None)

        self._queue: list[CachedToken] = []
        self._lock = threading.Lock()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._produced_since_restart = 0
        self._served = 0
        self._expired = 0
        self._proxy_unhealthy_reported = False
        self._last_error = ""

    # ------------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._refill_loop,
            name="sidecar-token-pool",
            daemon=True,
        )
        self._worker_thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._running = False
        thread = self._worker_thread
        self._worker_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)

    # ------------------------------------------------------------------ 内部实现

    def _purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            kept: list[CachedToken] = []
            for item in self._queue:
                if item.age(now) < self.max_age_sec:
                    kept.append(item)
                else:
                    self._expired += 1
            self._queue = kept

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "queued": len(self._queue),
                "served": self._served,
                "expired": self._expired,
                "consecutive_failures": self._consecutive_failures,
                "produced_since_restart": self._produced_since_restart,
                "worker_tokens_produced": self.worker.tokens_produced,
                "last_error": self._last_error,
                "proxy_unhealthy": self._proxy_unhealthy_reported,
            }

    def stats(self) -> dict[str, Any]:
        return self._snapshot()

    def _register_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._produced_since_restart += 1
            self._proxy_unhealthy_reported = False
        self.on_event("token_produced", self._snapshot())

    def _register_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
            self._last_error = f"{type(exc).__name__}: {exc}"
        self.on_event("token_failed", {"failures": failures, "error": self._last_error})
        if failures >= self.unhealthy_threshold and not self._proxy_unhealthy_reported:
            self._proxy_unhealthy_reported = True
            self.on_event(
                "proxy_unhealthy",
                {
                    "failures": failures,
                    "error": self._last_error,
                    "hint": "当前代理 IP 信誉过低或已被 Cloudflare 拦截，建议轮换出口",
                },
            )
            self._soft_restart("proxy_unhealthy")

    def _maybe_soft_restart(self) -> None:
        with self._lock:
            produced = self._produced_since_restart
        if produced >= self.restart_after:
            self._soft_restart("token_budget")

    def _soft_restart(self, reason: str) -> None:
        try:
            self.worker.restart_context()
        except Exception as exc:
            self._last_error = f"restart failed: {exc}"
            self.on_event("context_restart_failed", {"reason": reason, "error": str(exc)})
            return
        with self._lock:
            self._produced_since_restart = 0
        self.on_event("context_restarted", {"reason": reason})

    def _refill_loop(self) -> None:
        while self._running:
            try:
                self._purge_expired()
                with self._lock:
                    need = self.pool_size - len(self._queue)
                if need <= 0:
                    time.sleep(0.3)
                    continue
                token = self.worker.produce_turnstile_token(
                    self.action,
                    timeout=self.produce_timeout,
                )
                with self._lock:
                    self._queue.append(CachedToken(token, time.time()))
                self._register_success()
                self._maybe_soft_restart()
            except InteractiveChallengeError as exc:
                self._register_failure(exc)
                time.sleep(2.0)
            except (TurnstileChallengeError, SidecarError) as exc:
                self._register_failure(exc)
                time.sleep(1.0)
            except Exception as exc:  # 兜底，避免补水线程静默死亡
                self._register_failure(exc)
                time.sleep(1.0)

    # ------------------------------------------------------------------ 业务消费

    def get_turnstile_token(self, timeout: float = 30.0) -> str:
        """从池中取出一个有效 Token；池空则等待补水，最终降级为实时生成。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            self._purge_expired()
            with self._lock:
                if self._queue:
                    item = self._queue.pop(0)
                    self._served += 1
                    return item.value
            time.sleep(0.1)
        self.on_event("pool_starved", self._snapshot())
        return self.worker.produce_turnstile_token(self.action, timeout=self.produce_timeout)

    def get_castle_token(self, attempts: int = 2) -> str:
        """实时提取 Castle Request Token（强绑定当前真实设备会话）。"""
        last_error: Optional[BaseException] = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                token = self.worker.produce_castle_token()
                self.on_event("castle_produced", {"attempt": attempt})
                return token
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
        raise SidecarError(f"Castle Token 获取失败: {last_error}")
