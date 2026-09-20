#!/usr/bin/env python3
"""Generate a coherent per-session HTTP client profile for protocol requests.

关键约束：**UA 主版本必须与 curl_cffi 的 TLS/HTTP2 指纹版本一致**。
因此这里不再独立随机 UA 版本，而是先从 curl_cffi 实际支持的 impersonate
目标中挑选一个，再由该目标反推 UA 主版本，避免出现 "UA 是 Chrome 135、
TLS 是 Chrome 120" 的指纹倒挂。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
from typing import Any


# 指纹参数（可在 .env 中通过同名小写键覆盖部分项）
FINGERPRINT_PLATFORMS = ("windows", "macos", "linux")
FINGERPRINT_CHROME_MIN = 131
FINGERPRINT_CHROME_MAX = 150
FINGERPRINT_FIXED_PLATFORM = "windows"
FINGERPRINT_MODE = "random"
FINGERPRINT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
# 各区域默认语言偏好：可用 ACCEPT_LANGUAGE / FINGERPRINT_ACCEPT_LANGUAGE 覆盖
FINGERPRINT_ACCEPT_LANGUAGE_BY_REGION = {
    "us": "en-US,en;q=0.9",
    "gb": "en-GB,en;q=0.9",
    "de": "de-DE,de;q=0.9,en;q=0.8",
    "fr": "fr-FR,fr;q=0.9,en;q=0.8",
    "jp": "ja-JP,ja;q=0.9,en;q=0.8",
    "kr": "ko-KR,ko;q=0.9,en;q=0.8",
    "sg": "en-SG,en;q=0.9",
    "hk": "zh-HK,zh;q=0.9,en;q=0.8",
    "tw": "zh-TW,zh;q=0.9,en;q=0.8",
    "cn": "zh-CN,zh;q=0.9,en;q=0.8",
}
# Chrome 的 GREASE 品牌占位串会随版本轮换，写死单一取值是明显的脚本特征。
GREASE_BRANDS = (
    '"Not_A Brand";v="8"',
    '"Not)A;Brand";v="99"',
    '"Not/A)Brand";v="8"',
    '"Not_A Brand";v="24"',
    '"Not;A=Brand";v="99"',
    '"Not?A_Brand";v="24"',
)
# curl_cffi 不可用时的静态兜底（版本号来自 curl_cffi 的 impersonate 目标表）
FALLBACK_IMPERSONATE_TARGETS = (
    "chrome120",
    "chrome123",
    "chrome124",
    "chrome131",
    "chrome133a",
    "chrome136",
    "chrome142",
    "chrome145",
    "chrome146",
    "chrome150",
)

_TARGET_RE = re.compile(r"^chrome(\d+)([a-z]?)$")


@dataclass(frozen=True)
class FingerprintProfile:
    profile_id: str
    mode: str
    platform: str
    browser_major: int
    user_agent: str
    accept_language: str
    sec_ch_ua: str
    sec_ch_ua_mobile: str
    sec_ch_ua_platform: str
    impersonate: str = "chrome"

    def headers(self) -> dict[str, str]:
        return {
            "user-agent": self.user_agent,
            "accept-language": self.accept_language,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": self.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
        }

    def public_metadata(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "mode": self.mode,
            "platform": self.platform,
            "browser_major": self.browser_major,
            "accept_language": self.accept_language,
            "impersonate": self.impersonate,
        }


def _platforms(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = []
    allowed = tuple(
        item.strip().lower()
        for item in raw
        if item.strip().lower() in {"windows", "macos", "linux"}
    )
    return allowed or ("windows", "macos", "linux")


def impersonate_targets() -> tuple[str, ...]:
    """返回当前 curl_cffi 实际支持的桌面 Chrome impersonate 目标。"""
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral  # type: ignore
    except Exception:
        return FALLBACK_IMPERSONATE_TARGETS
    import typing

    try:
        candidates = typing.get_args(BrowserTypeLiteral)
    except Exception:
        return FALLBACK_IMPERSONATE_TARGETS
    targets = tuple(
        target
        for target in candidates
        if isinstance(target, str) and _TARGET_RE.match(target)
    )
    return targets or FALLBACK_IMPERSONATE_TARGETS


def _target_major(target: str) -> int:
    match = _TARGET_RE.match(target)
    return int(match.group(1)) if match else 0


def pick_impersonate_target(
    minimum: int = FINGERPRINT_CHROME_MIN,
    maximum: int = FINGERPRINT_CHROME_MAX,
) -> str:
    """在版本窗口内挑选一个 impersonate 目标，保证 TLS 与 UA 同版本。"""
    targets = impersonate_targets()
    window = [t for t in targets if minimum <= _target_major(t) <= maximum]
    if not window:
        # 窗口内无匹配时取最接近最大值的可用目标，避免退化成泛化 "chrome"
        window = sorted(targets, key=_target_major)[-3:]
    return secrets.choice(window)


def _major_from_user_agent(user_agent: str) -> int:
    match = re.search(r"(?:Chrome|CriOS)/(\d+)", user_agent)
    return int(match.group(1)) if match else 131


def _user_agent(platform: str, major: int) -> tuple[str, str]:
    if platform == "macos":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
        hint = '"macOS"'
    elif platform == "linux":
        os_part = "X11; Linux x86_64"
        hint = '"Linux"'
    else:
        os_part = "Windows NT 10.0; Win64; x64"
        hint = '"Windows"'
    user_agent = (
        f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    return user_agent, hint


def _sec_ch_ua(major: int) -> str:
    """构造 sec-ch-ua：GREASE 品牌随机轮换 + 品牌顺序随机打乱。"""
    brands = [
        f'"Chromium";v="{major}"',
        f'"Google Chrome";v="{major}"',
        secrets.choice(GREASE_BRANDS),
    ]
    # Chromium 会打乱品牌顺序，这里同样随机化，避免固定序列
    secrets.SystemRandom().shuffle(brands)
    return ", ".join(brands)


def _accept_language(config: dict[str, Any]) -> str:
    explicit = str(
        config.get("fingerprint_accept_language")
        or config.get("accept_language")
        or ""
    ).strip()
    if explicit:
        return explicit
    region = str(config.get("fingerprint_region") or "").strip().lower()
    if region in FINGERPRINT_ACCEPT_LANGUAGE_BY_REGION:
        return FINGERPRINT_ACCEPT_LANGUAGE_BY_REGION[region]
    return FINGERPRINT_ACCEPT_LANGUAGE


def build_fingerprint(config: dict[str, Any]) -> FingerprintProfile:
    mode = str(config.get("fingerprint_mode") or FINGERPRINT_MODE).strip().lower()
    accept_language = _accept_language(config)
    user_agent_override = str(config.get("user_agent") or "").strip()
    if mode == "fixed" and user_agent_override:
        user_agent = user_agent_override
        platform = str(config.get("fingerprint_platform") or FINGERPRINT_FIXED_PLATFORM).strip().lower()
        if platform not in {"windows", "macos", "linux"}:
            platform = "windows"
        major = _major_from_user_agent(user_agent)
        _, platform_hint = _user_agent(platform, major)
        # 固定 UA 时同样对齐 TLS 目标版本，避免 UA/TLS 版本倒挂
        target = f"chrome{major}"
        if target not in impersonate_targets():
            target = pick_impersonate_target()
    else:
        mode = "random"
        platform = secrets.choice(_platforms(config.get("fingerprint_platforms") or FINGERPRINT_PLATFORMS))
        target = pick_impersonate_target()
        major = _target_major(target)
        user_agent, platform_hint = _user_agent(platform, major)
    return FingerprintProfile(
        profile_id=secrets.token_hex(6),
        mode=mode,
        platform=platform,
        browser_major=major,
        user_agent=user_agent,
        accept_language=accept_language,
        sec_ch_ua=_sec_ch_ua(major),
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform=platform_hint,
        impersonate=target,
    )
