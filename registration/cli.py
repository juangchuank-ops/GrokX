#!/usr/bin/env python3
"""Browser-free CLI entrypoint for the protocol registration pipeline."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.loader import load_env
from registration.protocol_client import AuthProtocolClient
from providers.capsolver import CapSolverProvider
from providers.castle import CastleSdkTokenProvider, CastleTokenProvider, MoeMailProvider
from network.fingerprint import build_fingerprint
from registration.flow import (
    ProtocolRegistrationConfig,
    ProtocolRegistrationFlow,
)
from network.proxy import normalize_proxy_url, redact_proxy_url

PROTOCOL_TARGET_BASE = "https://accounts.x.ai"
# 以下两个 key 属于前端随时可能轮换的公钥：优先读 .env，缺省才用内置值
DEFAULT_TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_CASTLE_PUBLISHABLE_KEY = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"

STAGE_LABELS = {
    "init": "初始化注册任务",
    "protocol_session_bootstrapped": "建立协议会话",
    "email_created": "创建临时邮箱",
    "email_anti_abuse_token_ready": "生成邮件阶段 Castle Token",
    "email_code_requested": "发送邮箱验证码",
    "email_code_received": "获取邮箱验证码",
    "email_code_verified": "确认邮箱验证码",
    "turnstile_token_ready": "完成人机验证",
    "final_anti_abuse_token_ready": "生成注册阶段 Castle Token",
    "create_session_rpc_completed": "提交账号注册请求",
    "session_token_ready": "获取 SSO 凭据",
}
STAGE_NUMBERS = {stage: index for index, stage in enumerate(STAGE_LABELS, start=1)}

_RESULT_LOCK = threading.Lock()


def load_config(path: str = "") -> dict:
    return load_env(path or None)


def turnstile_sitekey(config: dict) -> str:
    return str(config.get("protocol_turnstile_sitekey") or DEFAULT_TURNSTILE_SITEKEY).strip()


def castle_publishable_key(config: dict) -> str:
    return str(config.get("protocol_castle_publishable_key") or DEFAULT_CASTLE_PUBLISHABLE_KEY).strip()


def use_local_sidecar(config: dict) -> bool:
    return bool(config.get("use_local_sidecar", False))


def missing_slots(config: dict) -> list[str]:
    checks = {
        "MOEMAIL_API_BASE": config.get("moemail_api_base"),
        "MOEMAIL_API_KEY": config.get("moemail_api_key"),
    }
    if use_local_sidecar(config):
        # 本地 Sidecar 取代 CapSolver，无需打码 API Key
        if not turnstile_sitekey(config):
            checks["PROTOCOL_TURNSTILE_SITEKEY"] = ""
    else:
        checks["CAPSOLVER_API_KEY"] = config.get("capsolver_api_key")
    if not (
        config.get("castle_provider_url")
        or (config.get("castle_email_token") and config.get("castle_final_token"))
        or castle_publishable_key(config)
        or use_local_sidecar(config)
    ):
        checks["CASTLE_TOKEN_PROVIDER"] = ""
    return [name for name, value in checks.items() if not str(value or "").strip()]


def write_result_json(path: str | Path, result) -> Path:
    """将注册账号追加到 JSON 数组，并将 SSO 追加到同名 TXT。"""
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "email": str(result.email),
        "password": str(result.password),
        "sso": str(result.session_token),
    }
    with _RESULT_LOCK:
        records = []
        if output.exists():
            try:
                existing = json.loads(output.read_text(encoding="utf-8-sig"))
                if isinstance(existing, list):
                    records = existing
                elif isinstance(existing, dict):
                    records = [{key: existing[key] for key in ("created_at", "email", "password", "sso") if key in existing}]
            except (OSError, ValueError):
                records = []
        records.append(payload)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)

        txt_output = output.with_suffix(".txt")
        token = payload["sso"].strip()
        existing_tokens = set(txt_output.read_text(encoding="utf-8-sig").splitlines()) if txt_output.exists() else set()
        if token and token not in existing_tokens:
            with txt_output.open("a", encoding="utf-8", newline="") as handle:
                handle.write(token + "\n")
    return output


def emit_event(enabled: bool, event: str, **fields) -> None:
    """Emit one flush-safe JSONL event without exposing credentials."""
    if enabled:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def report_progress(events: bool, stage: str, task_id: int = 0) -> None:
    if events:
        emit_event(True, "progress", stage=stage, task=task_id)
        return
    label = STAGE_LABELS.get(stage, stage)
    step = STAGE_NUMBERS.get(stage, 0)
    step_prefix = f"[步骤 {step}/{len(STAGE_LABELS)}]" if step else "[步骤]"
    prefix = f"[任务 {task_id}]{step_prefix}" if task_id else step_prefix
    print(f"{prefix} {label}...", flush=True)


def report_retry(events: bool, task_id: int, label: str, attempt: int, delay: float, error: str) -> None:
    if events:
        emit_event(True, "retry", task=task_id, step=label, attempt=attempt, delay=round(delay, 2), error=error)
        return
    print(
        f"[任务 {task_id}] 步骤 {label} 第 {attempt} 次失败（{error}），{delay:.1f}s 后重试",
        flush=True,
    )


def build_anti_abuse(config: dict, page_url: str, fingerprint, sidecar):
    """按优先级选择反滥用 Token 来源：本地 Sidecar > 远程/静态 Token > Node SDK。"""
    if sidecar is not None:
        from providers.local_sidecar import LocalCastleProvider

        return LocalCastleProvider(sidecar)
    if config.get("castle_provider_url") or config.get("castle_email_token") or config.get("castle_final_token"):
        return CastleTokenProvider(
            email_token=str(config.get("castle_email_token") or ""),
            final_token=str(config.get("castle_final_token") or ""),
            provider_url=str(config.get("castle_provider_url") or ""),
            provider_key=str(config.get("castle_provider_key") or ""),
        )
    return CastleSdkTokenProvider(castle_publishable_key(config), page_url, fingerprint.user_agent)


def build_human_verification(config: dict, fingerprint, proxies, sidecar):
    """按优先级选择人机验证来源：本地 Sidecar > CapSolver。"""
    if sidecar is not None:
        from providers.local_sidecar import LocalTurnstileProvider

        return LocalTurnstileProvider(
            sidecar,
            timeout=float(config.get("sidecar_turnstile_timeout", 30) or 30),
        )
    return CapSolverProvider(
        str(config.get("capsolver_api_key") or ""),
        timeout=float(config.get("capsolver_timeout_sec", 120) or 120),
        poll_interval=float(config.get("capsolver_poll_interval_sec", 1.0) or 1.0),
        proxy=str(config.get("proxy") or "") if config.get("proxy_enabled", True) else "",
        user_agent=fingerprint.user_agent,
    )


def start_local_sidecar(config: dict, proxies, fingerprint) -> tuple[Any, str]:
    """尝试启动本地 Sidecar；返回 (service|None, 失败原因)。"""
    from sidecar.service import LocalSidecarService

    proxy_url = ""
    if proxies:
        proxy_url = str(proxies.get("https") or proxies.get("http") or "")
    return LocalSidecarService.try_get_instance(
        proxy=proxy_url,
        user_agent=fingerprint.user_agent,
        sitekey=turnstile_sitekey(config),
        castle_pk=castle_publishable_key(config),
        action=str(config.get("protocol_turnstile_action") or ""),
        headless=bool(config.get("sidecar_headless", True)),
        pool_size=int(config.get("sidecar_pool_size", 2) or 2),
        max_age_sec=float(config.get("sidecar_max_age_sec", 240) or 240),
        browser_channel=str(config.get("sidecar_browser_channel") or ""),
        locale=str(config.get("sidecar_locale") or "en-US"),
        timezone_id=str(config.get("sidecar_timezone") or "America/New_York"),
    )


def run_web_task(
    task_id: int,
    config: dict,
    proxies,
    output_json: str,
    events: bool,
    *,
    save_result: bool = True,
    sidecar: Any = None,
):
    fingerprint = build_fingerprint(config)
    emit_event(events, "fingerprint", task=task_id, **fingerprint.public_metadata())
    base = PROTOCOL_TARGET_BASE
    page_url = str(config.get("protocol_page_url") or base + "/sign-up")
    client = AuthProtocolClient(
        base,
        proxies=proxies,
        user_agent=fingerprint.user_agent,
        default_headers=fingerprint.headers(),
        impersonate=fingerprint.impersonate,
    )
    flow = ProtocolRegistrationFlow(
        config=ProtocolRegistrationConfig(
            page_url=page_url,
            sitekey=turnstile_sitekey(config),
            action=str(config.get("protocol_turnstile_action") or ""),
            tos_accepted_version=int(config.get("protocol_tos_accepted_version", 1) or 1),
            step_attempts=int(config.get("step_attempts", 3) or 3),
            step_backoff=float(config.get("step_backoff", 1.5) or 1.5),
            max_step_backoff=float(config.get("max_step_backoff", 8.0) or 8.0),
        ),
        client=client,
        mail=MoeMailProvider(
            str(config["moemail_api_base"]),
            str(config["moemail_api_key"]),
            domain=str(config.get("moemail_domain") or ""),
            expiry_time=int(config.get("moemail_expiry_time", 86_400_000) or 86_400_000),
            proxies=proxies if config.get("moemail_use_proxy") else None,
        ),
        anti_abuse=build_anti_abuse(config, page_url, fingerprint, sidecar),
        human_verification=build_human_verification(config, fingerprint, proxies, sidecar),
        on_progress=lambda stage: report_progress(events, stage, task_id),
        on_retry=lambda label, attempt, delay, error: report_retry(events, task_id, label, attempt, delay, error),
    )
    result = flow.run()
    result.web_user_agent = fingerprint.user_agent
    output = write_result_json(output_json, result) if save_result else None
    return result, output


def sidecar_check(config: dict, proxies, events: bool, produce: bool = False) -> int:
    """诊断本地 Sidecar：启动浏览器、加载 Harness，可选地真实产出一次 Token。"""
    fingerprint = build_fingerprint(config)
    service, reason = start_local_sidecar(config, proxies, fingerprint)
    if service is None:
        payload = {"success": False, "error": reason}
        emit_event(events, "sidecar_check", **payload)
        if not events:
            print(json.dumps(payload, ensure_ascii=False))
        return 1
    try:
        payload: dict[str, Any] = {"success": True, **service.status()}
        if produce:
            timeout = float(config.get("sidecar_turnstile_timeout", 30) or 30)
            started = time.monotonic()
            turnstile = service.acquire_turnstile(timeout=timeout)
            castle = service.acquire_castle()
            payload.update(
                {
                    "turnstile_token_length": len(turnstile),
                    "castle_token_length": len(castle),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
    except Exception as exc:
        payload = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        service.stop()
        from sidecar.service import LocalSidecarService

        LocalSidecarService.reset()
    emit_event(events, "sidecar_check", **payload)
    if not events:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("success") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Pure HTTP/gRPC-Web registration flow")
    parser.add_argument("--env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("-n", "--count", type=int, default=1, help="注册数量")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="并发任务数")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--proxy-check", action="store_true")
    parser.add_argument("--sidecar-check", action="store_true", help="诊断本地无头浏览器 Sidecar 是否可用")
    parser.add_argument(
        "--sidecar-produce",
        action="store_true",
        help="配合 --sidecar-check：真实产出一次 Turnstile + Castle Token（只输出长度）",
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="输出脱敏后的 JSONL 进度事件",
    )
    parser.add_argument(
        "--output-json",
        default=str(Path(__file__).resolve().parents[1] / "output" / "web_register_result.json"),
        help="credential result JSON path (written atomically)",
    )
    args = parser.parse_args()
    if args.count < 1 or args.jobs < 1:
        parser.error("-n/--count 和 -j/--jobs 必须大于 0")
    args.jobs = min(args.jobs, args.count)
    config = load_config(args.env)
    proxy_enabled = bool(config.get("proxy_enabled", True))
    proxy = str(config.get("proxy") or "").strip() if proxy_enabled else ""
    proxies = None
    if proxy:
        normalized = normalize_proxy_url(proxy)
        proxies = {"http": normalized, "https": normalized}
        if not args.events:
            print(f"[网络] 使用代理: {redact_proxy_url(normalized)}", flush=True)
    elif not args.events:
        print("[网络] 使用直连", flush=True)

    fingerprint = build_fingerprint(config)

    if args.proxy_check:
        if not proxy:
            emit_event(args.events, "proxy_test", success=True, mode="direct", status=0)
            if not args.events:
                print(json.dumps({"success": True, "mode": "direct", "status": 0}))
            return 0
        from curl_cffi import requests

        started = time.monotonic()
        try:
            response = requests.get(
                str(config.get("protocol_page_url") or PROTOCOL_TARGET_BASE),
                proxies=proxies,
                headers=fingerprint.headers(),
                impersonate=fingerprint.impersonate,
                timeout=15,
            )
            payload = {
                "success": 200 <= int(response.status_code) < 500,
                "mode": "proxy",
                "status": int(response.status_code),
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            payload = {
                "success": False,
                "mode": "proxy",
                "status": 0,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error_type": type(exc).__name__,
            }
        emit_event(args.events, "proxy_test", **payload)
        if not args.events:
            print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload["success"] else 1

    if args.sidecar_check:
        return sidecar_check(config, proxies, args.events, produce=args.sidecar_produce)

    missing = missing_slots(config)
    if args.check or missing:
        payload = {
            "browser": False,
            "sidecar": use_local_sidecar(config),
            "ready": not missing,
            "missing_slots": missing,
        }
        if args.events:
            emit_event(True, "check", **payload)
        else:
            print(json.dumps(payload, ensure_ascii=False))
        return 0 if args.check else 2

    sidecar = None
    if use_local_sidecar(config):
        sidecar, reason = start_local_sidecar(config, proxies, fingerprint)
        if sidecar is None:
            emit_event(args.events, "sidecar_unavailable", error=reason)
            if not args.events:
                print(f"[Sidecar] 本地无头浏览器不可用，降级到外部 Provider：{reason}", flush=True)
            if not str(config.get("capsolver_api_key") or "").strip():
                payload = {"browser": False, "ready": False, "missing_slots": ["CAPSOLVER_API_KEY"], "error": reason}
                emit_event(args.events, "check", **payload)
                if not args.events:
                    print(json.dumps(payload, ensure_ascii=False))
                return 2
        elif not args.events:
            print(
                f"[Sidecar] 已启动本地无头浏览器，池容量={int(config.get('sidecar_pool_size', 2) or 2)}，"
                f"Turnstile 与 Castle 均走同一条代理链路",
                flush=True,
            )

    success = 0
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {
                executor.submit(
                    run_web_task,
                    task_id,
                    config,
                    proxies,
                    args.output_json,
                    args.events,
                    sidecar=sidecar,
                ): task_id
                for task_id in range(1, args.count + 1)
            }
            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    _, output = future.result()
                    success += 1
                    emit_event(args.events, "task_complete", task=task_id, success=True)
                    if not args.events:
                        print(f"[任务 {task_id}] 注册成功", flush=True)
                except Exception as exc:
                    emit_event(args.events, "task_complete", task=task_id, success=False, error_type=type(exc).__name__, message=str(exc))
                    if not args.events:
                        print(f"[任务 {task_id}] 注册失败: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if sidecar is not None:
            try:
                emit_event(args.events, "sidecar_stats", **sidecar.pool.stats())
            except Exception:
                pass
            sidecar.stop()
            from sidecar.service import LocalSidecarService

            LocalSidecarService.reset()

    failed = args.count - success
    if args.events:
        emit_event(True, "batch_complete", total=args.count, success=success, failed=failed)
    else:
        output = Path(args.output_json).resolve()
        print(f"[完成] 总数={args.count} 成功={success} 失败={failed}", flush=True)
        print(f"[结果] JSON: {output}", flush=True)
        print(f"[结果] SSO TXT: {output.with_suffix('.txt')}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
