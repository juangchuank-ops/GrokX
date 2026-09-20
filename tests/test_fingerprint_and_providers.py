"""指纹一致性、CapSolver 代理透传与代理归一化测试。"""

from __future__ import annotations

import unittest

from network.fingerprint import (
    FINGERPRINT_CHROME_MAX,
    FINGERPRINT_CHROME_MIN,
    _target_major,
    build_fingerprint,
    impersonate_targets,
    pick_impersonate_target,
)
from network.proxy import normalize_proxy_url, parse_proxy_url, redact_proxy_url
from providers.capsolver import CapSolverProvider
from providers.turnstile_flow import ChallengeContext


class FingerprintTest(unittest.TestCase):
    def test_user_agent_matches_tls_target_version(self):
        """UA 主版本必须与 curl_cffi 的 impersonate 版本一致（缺陷 4）。"""
        for _ in range(30):
            profile = build_fingerprint({})
            self.assertEqual(
                _target_major(profile.impersonate),
                profile.browser_major,
                f"UA/TLS 版本倒挂: {profile.user_agent} vs {profile.impersonate}",
            )
            self.assertIn(f"Chrome/{profile.browser_major}.0.0.0", profile.user_agent)

    def test_target_within_configured_window(self):
        for _ in range(30):
            major = _target_major(pick_impersonate_target())
            self.assertGreaterEqual(major, FINGERPRINT_CHROME_MIN)
            self.assertLessEqual(major, FINGERPRINT_CHROME_MAX)

    def test_sec_ch_ua_is_not_hardcoded(self):
        """sec-ch-ua 的 GREASE 品牌与顺序必须轮换（缺陷 5）。"""
        samples = {build_fingerprint({}).sec_ch_ua for _ in range(60)}
        self.assertGreater(len(samples), 1)
        for value in samples:
            self.assertIn("Google Chrome", value)
            self.assertIn("Chromium", value)

    def test_accept_language_defaults_to_en_us(self):
        self.assertEqual("en-US,en;q=0.9", build_fingerprint({}).accept_language)

    def test_accept_language_region_and_explicit_override(self):
        self.assertEqual("ja-JP,ja;q=0.9,en;q=0.8", build_fingerprint({"fingerprint_region": "jp"}).accept_language)
        self.assertEqual("de-DE,de;q=0.9", build_fingerprint({"accept_language": "de-DE,de;q=0.9"}).accept_language)

    def test_fixed_mode_aligns_tls_to_ua(self):
        profile = build_fingerprint(
            {"fingerprint_mode": "fixed", "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136.0.0.0"}
        )
        self.assertEqual(136, profile.browser_major)
        self.assertIn(profile.impersonate, impersonate_targets())


class ProxyTest(unittest.TestCase):
    def test_socks5_is_upgraded_to_socks5h(self):
        self.assertEqual("socks5h://u:p@1.2.3.4:1080", normalize_proxy_url("socks5://u:p@1.2.3.4:1080"))

    def test_reversed_provider_format(self):
        spec = parse_proxy_url("http://1.2.3.4:8080@user:pass")
        self.assertEqual(("1.2.3.4", 8080, "user", "pass"), (spec.host, spec.port, spec.username, spec.password))

    def test_redact_hides_credentials(self):
        normalized = normalize_proxy_url("socks5://user:pass@1.2.3.4:1080")
        self.assertEqual("socks5h://use***:***@1.2.3.4:1080", redact_proxy_url(normalized))

    def test_error_message_no_longer_mentions_browser(self):
        with self.assertRaises(ValueError) as ctx:
            parse_proxy_url("ftp://1.2.3.4:21")
        self.assertNotIn("浏览器", str(ctx.exception))


class CapSolverTaskTest(unittest.TestCase):
    challenge = ChallengeContext(
        page_url="https://accounts.x.ai/sign-up",
        sitekey="0x4AAAAAAAhr9JGVDZbrZOo0",
        action="signup",
    )

    def test_proxy_is_forwarded_to_task(self):
        """缺陷 3：配置了代理就必须用带代理的任务类型。"""
        provider = CapSolverProvider(
            "key",
            proxy="socks5h://user:pass@1.2.3.4:1080",
            user_agent="UA/1.0",
        )
        task = provider._task_payload(self.challenge)
        self.assertEqual("AntiTurnstileTask", task["type"])
        self.assertEqual("socks5", task["proxyType"])
        self.assertEqual("1.2.3.4", task["proxyAddress"])
        self.assertEqual(1080, task["proxyPort"])
        self.assertEqual("user", task["proxyLogin"])
        self.assertEqual("pass", task["proxyPassword"])
        self.assertEqual("UA/1.0", task["userAgent"])
        self.assertEqual({"action": "signup"}, task["metadata"])

    def test_proxy_less_only_when_no_proxy_configured(self):
        task = CapSolverProvider("key")._task_payload(self.challenge)
        self.assertEqual("AntiTurnstileTaskProxyLess", task["type"])
        for key in ("proxyType", "proxyAddress", "proxyPort"):
            self.assertNotIn(key, task)

    def test_http_proxy_maps_to_http_type(self):
        provider = CapSolverProvider("key", proxy="http://1.2.3.4:8080")
        self.assertEqual("http", provider._task_payload(self.challenge)["proxyType"])

    def test_invalid_proxy_is_rejected(self):
        from providers.capsolver import CapSolverError

        provider = CapSolverProvider("key", proxy="http://1.2.3.4")
        with self.assertRaises(CapSolverError):
            provider._task_payload(self.challenge)


if __name__ == "__main__":
    unittest.main()
