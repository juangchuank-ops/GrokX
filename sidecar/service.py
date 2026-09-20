#!/usr/bin/env python3
"""sidecar/service.py: Sidecar 服务门面（单例 + 懒启动 + 降级信号）。

对外只暴露三件事：``acquire_turnstile`` / ``acquire_castle`` / ``stop``。
初始化失败时抛 ``SidecarUnavailable``，由 CLI 决定是否降级回外部 Provider。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from sidecar.browser_worker import BrowserWorker, SidecarUnavailable
from sidecar.token_pool import DEFAULT_MAX_AGE_SEC, TokenPool


class LocalSidecarService:
    """进程内单例：一个常驻浏览器实例 + 一个 Turnstile 缓冲池。"""

    _instance: Optional["LocalSidecarService"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        proxy: str = "",
        user_agent: str = "",
        *,
        sitekey: str = "0x4AAAAAAAhr9JGVDZbrZOo0",
        castle_pk: str = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz",
        action: str = "",
        headless: bool = True,
        pool_size: int = 2,
        max_age_sec: float = DEFAULT_MAX_AGE_SEC,
        browser_channel: str = "",
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ):
        self.proxy = str(proxy or "").strip()
        self.user_agent = str(user_agent or "").strip()
        self.sitekey = str(sitekey or "").strip()
        self.castle_pk = str(castle_pk or "").strip()
        self.action = str(action or "").strip()
        self.on_event = on_event
        self.worker = BrowserWorker(
            proxy=self.proxy,
            user_agent=self.user_agent,
            sitekey=self.sitekey,
            castle_pk=self.castle_pk,
            headless=headless,
            locale=locale,
            timezone_id=timezone_id,
            browser_channel=browser_channel,
        )
        self.pool = TokenPool(
            self.worker,
            pool_size=pool_size,
            max_age_sec=max_age_sec,
            action=self.action,
            on_event=on_event,
        )
        self._started = False
        self._start_lock = threading.Lock()

    # ------------------------------------------------------------------ 单例

    @classmethod
    def get_instance(cls, proxy: str = "", user_agent: str = "", **kwargs: Any) -> "LocalSidecarService":
        with cls._instance_lock:
            if cls._instance is None or cls._instance.worker.closed:
                cls._instance = cls(proxy, user_agent, **kwargs)
            return cls._instance

    @classmethod
    def try_get_instance(
        cls,
        proxy: str = "",
        user_agent: str = "",
        **kwargs: Any,
    ) -> tuple[Optional["LocalSidecarService"], str]:
        """尝试建立并启动 Sidecar；失败时返回 (None, 原因)，供 CLI 平滑降级。"""
        try:
            service = cls.get_instance(proxy, user_agent, **kwargs)
            service.start()
            return service, ""
        except SidecarUnavailable as exc:
            return None, str(exc)
        except Exception as exc:  # 兜底：任何启动异常都不应让整条流水线中断
            return None, f"{type(exc).__name__}: {exc}"

    @classmethod
    def reset(cls) -> None:
        with cls._instance_lock:
            instance = cls._instance
            cls._instance = None
        if instance is not None:
            instance.stop()

    # ------------------------------------------------------------------ 生命周期

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            # 先同步启动浏览器：启动失败必须立刻抛出，避免后台线程反复失败
            self.worker.start()
            self.pool.start()
            self._started = True

    def stop(self) -> None:
        with self._start_lock:
            if not self._started:
                return
            try:
                self.pool.stop()
            finally:
                self.worker.close()
                self._started = False

    def __enter__(self) -> "LocalSidecarService":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ 业务接口

    def acquire_turnstile(self, timeout: float = 30.0) -> str:
        return self.pool.get_turnstile_token(timeout=timeout)

    def acquire_castle(self, attempts: int = 2) -> str:
        return self.pool.get_castle_token(attempts=attempts)

    def status(self) -> dict[str, Any]:
        payload = {
            "started": self._started,
            "proxy": bool(self.proxy),
            "sitekey": bool(self.sitekey),
            "castle_pk": bool(self.castle_pk),
            "action": self.action,
            **self.pool.stats(),
        }
        if self._started:
            try:
                payload["harness"] = self.worker.status()
            except Exception as exc:
                payload["harness_error"] = f"{type(exc).__name__}: {exc}"
        return payload
