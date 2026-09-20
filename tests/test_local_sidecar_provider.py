"""本地 Sidecar Provider 适配层与 CLI 配置校验测试。"""

from __future__ import annotations

import unittest

from providers.local_sidecar import LocalCastleProvider, LocalTurnstileProvider, build_local_providers
from providers.turnstile_flow import ChallengeContext
from registration.cli import (
    DEFAULT_CASTLE_PUBLISHABLE_KEY,
    DEFAULT_TURNSTILE_SITEKEY,
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


if __name__ == "__main__":
    unittest.main()
