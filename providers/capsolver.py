#!/usr/bin/env python3
"""CapSolver provider for the project's Turnstile provider interface."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from curl_cffi import requests

from providers.turnstile_flow import AcquiredToken, ChallengeContext


CREATE_TASK_URL = "https://api.capsolver.com/createTask"
GET_TASK_RESULT_URL = "https://api.capsolver.com/getTaskResult"
TASK_TYPE_PROXYLESS = "AntiTurnstileTaskProxyLess"
TASK_TYPE_PROXY = "AntiTurnstileTask"
# CapSolver 的代理协议枚举与 URL scheme 的映射（它不认识 socks5h）
CAPSOLVER_PROXY_TYPES = {
    "http": "http",
    "https": "http",
    "socks4": "socks4",
    "socks4a": "socks4",
    "socks5": "socks5",
    "socks5h": "socks5",
}


class CapSolverError(RuntimeError):
    """Raised when CapSolver rejects or cannot finish a task."""

    def __init__(self, code: str, description: str = ""):
        self.code = str(code or "CAPSOLVER_ERROR")
        self.description = str(description or "")
        message = self.code
        if self.description:
            message += f": {self.description}"
        super().__init__(message)


class CapSolverProvider:
    """Acquire short-lived Turnstile tokens through CapSolver's task API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 120,
        poll_interval: float = 1.0,
        request_timeout: float = 20,
        session: Any = None,
        log_callback: Callable[[str], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
        proxy: str = "",
        user_agent: str = "",
    ):
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("CapSolver API key is empty")
        self.timeout = max(1.0, float(timeout))
        self.poll_interval = max(0.2, float(poll_interval))
        self.request_timeout = max(1.0, float(request_timeout))
        self.session = session or requests
        self.log = log_callback or (lambda _message: None)
        self.cancelled = cancel_callback or (lambda: False)
        self.proxy = str(proxy or "").strip()
        self.user_agent = str(user_agent or "").strip()

    @staticmethod
    def _raise_api_error(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise CapSolverError("INVALID_RESPONSE", "response is not a JSON object")
        error_id = int(data.get("errorId") or 0)
        if error_id:
            raise CapSolverError(
                str(data.get("errorCode") or f"ERROR_{error_id}"),
                str(data.get("errorDescription") or ""),
            )
        return data

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(url, json=payload, timeout=self.request_timeout)
            try:
                data = response.json()
            except Exception:
                response.raise_for_status()
                raise CapSolverError("INVALID_RESPONSE", "response body is not JSON")
            # CapSolver can return a useful errorCode together with HTTP 4xx.
            # Decode that first so callers receive the provider error rather
            # than a generic transport exception.
            parsed = self._raise_api_error(data)
            response.raise_for_status()
            return parsed
        except CapSolverError:
            raise
        except Exception as exc:
            raise CapSolverError("REQUEST_FAILED", str(exc)) from exc

    def _sleep(self) -> None:
        deadline = time.monotonic() + self.poll_interval
        while time.monotonic() < deadline:
            if self.cancelled():
                raise CapSolverError("CANCELLED", "task cancelled")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _proxy_fields(self) -> dict[str, Any]:
        """把已配置的代理转换成 CapSolver 的 proxy* 参数。

        未配置代理时返回空字典，此时才允许退化为 ProxyLess 任务类型。
        """
        if not self.proxy:
            return {}
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(self.proxy)
        scheme = (parts.scheme or "http").lower()
        proxy_type = CAPSOLVER_PROXY_TYPES.get(scheme)
        if not proxy_type:
            raise CapSolverError("UNSUPPORTED_PROXY", f"CapSolver 不支持代理协议 {scheme}")
        if not parts.hostname or not parts.port:
            raise CapSolverError("INVALID_PROXY", "代理地址缺少主机或端口")
        fields: dict[str, Any] = {
            "proxyType": proxy_type,
            "proxyAddress": parts.hostname,
            "proxyPort": int(parts.port),
        }
        if parts.username:
            fields["proxyLogin"] = unquote(parts.username)
        if parts.password:
            fields["proxyPassword"] = unquote(parts.password)
        return fields

    def _task_payload(self, challenge: ChallengeContext) -> dict[str, Any]:
        proxy_fields = self._proxy_fields()
        task: dict[str, Any] = {
            # 有代理时必须用带代理的任务类型，否则打码 IP 与注册 IP 会脱节
            "type": TASK_TYPE_PROXY if proxy_fields else TASK_TYPE_PROXYLESS,
            "websiteURL": challenge.page_url,
            "websiteKey": challenge.sitekey,
        }
        task.update(proxy_fields)
        if self.user_agent:
            task["userAgent"] = self.user_agent
        metadata = {
            key: value
            for key, value in {
                "action": str(challenge.action or "").strip(),
                "cdata": str(challenge.cdata or "").strip(),
            }.items()
            if value
        }
        if metadata:
            task["metadata"] = metadata
        return task

    def acquire(self, challenge: ChallengeContext) -> AcquiredToken:
        if self.cancelled():
            raise CapSolverError("CANCELLED", "task cancelled")
        if not str(challenge.page_url or "").strip():
            raise ValueError("Turnstile page URL is empty")
        if not str(challenge.sitekey or "").strip():
            raise ValueError("Turnstile sitekey is empty")

        task = self._task_payload(challenge)

        created = self._post(CREATE_TASK_URL, {"clientKey": self.api_key, "task": task})
        task_id = str(created.get("taskId") or "").strip()
        if not task_id:
            raise CapSolverError("MISSING_TASK_ID", "createTask returned no taskId")
        self.log(f"[*] CapSolver task created: {task_id} (type={task['type']})")

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.cancelled():
                raise CapSolverError("CANCELLED", "task cancelled")
            result = self._post(
                GET_TASK_RESULT_URL,
                {"clientKey": self.api_key, "taskId": task_id},
            )
            status = str(result.get("status") or "").strip().lower()
            if status == "ready":
                solution = result.get("solution") or {}
                token = str(solution.get("token") or "").strip()
                if not token or len(token) > 2048:
                    raise CapSolverError("INVALID_TOKEN", "ready task returned an invalid token")
                self.log(f"[*] CapSolver task ready: token_length={len(token)}")
                return AcquiredToken(token, source="capsolver")
            if status not in {"", "idle", "processing"}:
                raise CapSolverError("UNEXPECTED_STATUS", status)
            self._sleep()

        raise CapSolverError("TASK_TIMEOUT", f"task exceeded {self.timeout:g}s")
