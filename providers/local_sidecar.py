#!/usr/bin/env python3
"""providers/local_sidecar.py: 适配 GrokX 状态机调用的本地 Provider。

完全兼容 ``registration/flow.py`` 中的 ``HumanVerificationProvider`` 与
``AntiAbuseProvider`` 协议，可零改动替换 CapSolverProvider / CastleSdkTokenProvider。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from providers.turnstile_flow import AcquiredToken, ChallengeContext
from sidecar.service import LocalSidecarService


class LocalTurnstileProvider:
    """替代 CapSolverProvider：直接从本地 Sidecar 缓冲池获取 Turnstile Token。"""

    def __init__(self, sidecar: LocalSidecarService, *, timeout: float = 30.0):
        self.sidecar = sidecar
        self.timeout = float(timeout)

    def acquire(self, challenge: ChallengeContext) -> AcquiredToken:
        token = self.sidecar.acquire_turnstile(timeout=self.timeout)
        return AcquiredToken(token, source="local_headless_sidecar")


class LocalCastleProvider:
    """替代 CastleSdkTokenProvider：从真实浏览器上下文提取高可信 Castle Token。"""

    def __init__(self, sidecar: LocalSidecarService, *, attempts: int = 2):
        self.sidecar = sidecar
        self.attempts = max(1, int(attempts))

    def acquire(self, *, stage: str, email: str) -> str:
        # 发信与注册两个阶段各取一次真实设备 Token
        return self.sidecar.acquire_castle(attempts=self.attempts)


def build_local_providers(
    sidecar: LocalSidecarService,
    *,
    turnstile_timeout: float = 30.0,
) -> tuple[LocalTurnstileProvider, LocalCastleProvider]:
    return (
        LocalTurnstileProvider(sidecar, timeout=turnstile_timeout),
        LocalCastleProvider(sidecar),
    )


class FallbackTurnstileProvider:
    """Sidecar 求解失败时回退到备用 Provider（如 CapSolver），保证流水线不中断。"""

    def __init__(
        self,
        primary: LocalTurnstileProvider,
        fallback: Any,
        *,
        on_fallback: Optional[Callable[[str, BaseException], None]] = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.on_fallback = on_fallback or (lambda _reason, _exc: None)

    def acquire(self, challenge: ChallengeContext) -> AcquiredToken:
        try:
            return self.primary.acquire(challenge)
        except Exception as exc:
            self.on_fallback("turnstile", exc)
            return self.fallback.acquire(challenge)


class FallbackAntiAbuseProvider:
    """Sidecar 提取 Castle 令牌失败时回退到 Node SDK / 远程供应商。"""

    def __init__(
        self,
        primary: LocalCastleProvider,
        fallback: Any,
        *,
        on_fallback: Optional[Callable[[str, BaseException], None]] = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.on_fallback = on_fallback or (lambda _reason, _exc: None)

    def acquire(self, *, stage: str, email: str) -> str:
        try:
            return self.primary.acquire(stage=stage, email=email)
        except Exception as exc:
            self.on_fallback("anti_abuse", exc)
            return self.fallback.acquire(stage=stage, email=email)
