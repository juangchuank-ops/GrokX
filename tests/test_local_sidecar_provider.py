"""本地 Sidecar Provider 适配层与 CLI 配置校验测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from providers.local_sidecar import (
    FallbackAntiAbuseProvider,
    FallbackTurnstileProvider,
    LocalCastleProvider,
    LocalTurnstileProvider,
    build_local_providers,
)
from providers.turnstile_flow import AcquiredToken, ChallengeContext
from registration.cli import (
    DEFAULT_CASTLE_PUBLISHABLE_KEY,
    DEFAULT_TURNSTILE_SITEKEY,
    build_anti_abuse,
    build_human_verification,
    castle_publishable_key,
    missing_slots,
    turnstile_sitekey,
)


class _FakeSidecar:
    def __init__(self):
        self.turnstile_calls = 0
        self.castle_calls = 0

    def acquire_turnstile(self, timeout: float = 30.0) -> str:
        self.turnstile_calls += 1
        return "ts-token"

    def acquire_castle(self, attempts: int = 2) -> str:
        self.castle_calls += 1
        return "castle-token"


class LocalProviderTest(unittest.TestCase):
    challenge = ChallengeContext(page_url="https://accounts.x.ai/sign-up", sitekey="site-key")

    def test_turnstile_provider_returns_acquired_token(self):
        sidecar = _FakeSidecar()
        token = LocalTurnstileProvider(sidecar).acquire(self.challenge)
        self.assertEqual("ts-token", token.value)
        self.assertEqual("local_headless_sidecar", token.source)

    def test_castle_provider_satisfies_anti_abuse_protocol(self):
        sidecar = _FakeSidecar()
        provider = LocalCastleProvider(sidecar)
        self.assertEqual("castle-token", provider.acquire(stage="email", email="a@b.com"))
        self.assertEqual("castle-token", provider.acquire(stage="final", email="a@b.com"))
        self.assertEqual(2, sidecar.castle_calls)

    def test_factory_returns_both_providers(self):
        turnstile, castle = build_local_providers(_FakeSidecar())
        self.assertIsInstance(turnstile, LocalTurnstileProvider)
        self.assertIsInstance(castle, LocalCastleProvider)


class ConfigTest(unittest.TestCase):
    def test_sitekey_falls_back_to_builtin_default(self):
        self.assertEqual(DEFAULT_TURNSTILE_SITEKEY, turnstile_sitekey({}))
        self.assertEqual("0xCustom", turnstile_sitekey({"protocol_turnstile_sitekey": "0xCustom"}))
        self.assertEqual(DEFAULT_CASTLE_PUBLISHABLE_KEY, castle_publishable_key({}))

    def test_capsolver_key_not_required_when_sidecar_enabled(self):
        config = {
            "moemail_api_base": "https://mail.example.com",
            "moemail_api_key": "k",
            "use_local_sidecar": True,
        }
        self.assertEqual([], missing_slots(config))

    def test_capsolver_key_required_in_legacy_mode(self):
        config = {
            "moemail_api_base": "https://mail.example.com",
            "moemail_api_key": "k",
            "protocol_castle_publishable_key": "pk_x",
        }
        self.assertIn("CAPSOLVER_API_KEY", missing_slots(config))

    def test_mail_configuration_is_always_required(self):
        self.assertIn("MOEMAIL_API_KEY", missing_slots({"use_local_sidecar": True}))


class FallbackProviderTest(unittest.TestCase):
    """文档 §7.2 的混合双轨容灾。"""

    challenge = ChallengeContext(page_url="https://accounts.x.ai/sign-up", sitekey="site-key")

    class _BrokenSidecar:
        def acquire_turnstile(self, timeout: float = 30.0) -> str:
            raise RuntimeError("sidecar turnstile failed")

        def acquire_castle(self, attempts: int = 2) -> str:
            raise RuntimeError("sidecar castle failed")

    class _Fallback:
        def __init__(self):
            self.turnstile_calls = 0
            self.castle_calls = 0

        def acquire(self, challenge=None, *, stage="", email=""):
            if challenge is not None:
                self.turnstile_calls += 1
                return AcquiredToken("fallback-ts", source="fallback")
            self.castle_calls += 1
            return "fallback-castle"

    def test_turnstile_falls_back_on_sidecar_failure(self):
        events: list[str] = []
        fallback = self._Fallback()
        provider = FallbackTurnstileProvider(
            LocalTurnstileProvider(self._BrokenSidecar()),
            fallback,
            on_fallback=lambda kind, exc: events.append(kind),
        )
        token = provider.acquire(self.challenge)
        self.assertEqual("fallback-ts", token.value)
        self.assertEqual(1, fallback.turnstile_calls)
        self.assertEqual(["turnstile"], events)

    def test_anti_abuse_falls_back_on_sidecar_failure(self):
        events: list[str] = []
        fallback = self._Fallback()
        provider = FallbackAntiAbuseProvider(
            LocalCastleProvider(self._BrokenSidecar()),
            fallback,
            on_fallback=lambda kind, exc: events.append(kind),
        )
        self.assertEqual("fallback-castle", provider.acquire(stage="email", email="a@b.com"))
        self.assertEqual(1, fallback.castle_calls)
        self.assertEqual(["anti_abuse"], events)

    def test_primary_success_does_not_touch_fallback(self):
        sidecar = _FakeSidecar()
        fallback = self._Fallback()
        provider = FallbackTurnstileProvider(LocalTurnstileProvider(sidecar), fallback)
        self.assertEqual("ts-token", provider.acquire(self.challenge).value)
        self.assertEqual(0, fallback.turnstile_calls)


class ProviderRoutingTest(unittest.TestCase):
    fingerprint = type("FP", (), {"user_agent": "UA", "impersonate": "chrome"})()

    def test_sidecar_with_capsolver_key_wraps_fallback(self):
        config = {"capsolver_api_key": "k", "protocol_turnstile_sitekey": "sk"}
        provider = build_human_verification(config, self.fingerprint, _FakeSidecar())
        self.assertIsInstance(provider, FallbackTurnstileProvider)

    def test_sidecar_without_capsolver_key_is_standalone(self):
        provider = build_human_verification({}, self.fingerprint, _FakeSidecar())
        self.assertIsInstance(provider, LocalTurnstileProvider)

    def test_anti_abuse_prefers_static_tokens_as_fallback(self):
        config = {"castle_email_token": "e", "castle_final_token": "f"}
        provider = build_anti_abuse(
            config,
            "https://accounts.x.ai/sign-up",
            self.fingerprint,
            _FakeSidecar(),
        )
        self.assertIsInstance(provider, FallbackAntiAbuseProvider)

    def test_missing_all_castle_sources_raises_actionable_error(self):
        with patch("registration.cli._legacy_anti_abuse", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                build_anti_abuse({}, "https://accounts.x.ai/sign-up", self.fingerprint, None)
        self.assertIn("USE_LOCAL_SIDECAR", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
