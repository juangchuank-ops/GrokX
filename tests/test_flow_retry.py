"""状态机步骤级重试与账号去特征化测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from registration.flow import (
    FAMILY_NAME_POOL,
    GIVEN_NAME_POOL,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    ProtocolRegistrationConfig,
    ProtocolRegistrationFlow,
    generate_password,
    generate_profile,
    is_retryable_error,
)
from registration.protocol_client import ProtocolError


class _FlakyClient:
    """指定方法在第 N 次调用前失败，用于验证步骤级重试。"""

    def __init__(self, failures: int = 1, error: Exception | None = None, methods: tuple[str, ...] = ("create_email_code",)):
        self.failures = failures
        self.error = error or ProtocolError("gRPC status 14: unavailable")
        self.methods = methods
        self.calls: list[str] = []
        self.attempts: dict[str, int] = {}

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        self.attempts[name] = self.attempts.get(name, 0) + 1
        if name in self.methods and self.attempts[name] <= self.failures:
            raise self.error

    def bootstrap(self, page_url):
        self._hit("bootstrap")

    def create_email_validation_code(self, email, *, castle_request_token):
        self._hit("create_email_code")

    def verify_email_validation_code(self, email, code, **kwargs):
        self._hit("verify_email_code")

    def create_user_and_session(self, **kwargs):
        self._hit("create_user")
        return SimpleNamespace(response=SimpleNamespace(cookies={"sso": "sso-secret"}), messages=[])


class _Mail:
    def __init__(self):
        self.calls = 0

    def create(self):
        return "test@example.com", "mail-token"

    def wait_code(self, token, email):
        self.calls += 1
        return "123456"


class _AntiAbuse:
    def acquire(self, *, stage, email):
        return f"castle-{stage}"


class _HumanVerification:
    def acquire(self, challenge):
        return "turnstile-token"


def _flow(client, *, retries: list | None = None, attempts: int = 3) -> ProtocolRegistrationFlow:
    return ProtocolRegistrationFlow(
        config=ProtocolRegistrationConfig(
            page_url="https://accounts.x.ai/sign-up",
            sitekey="site-key",
            step_attempts=attempts,
            step_backoff=0.0,
            max_step_backoff=0.0,
        ),
        client=client,
        mail=_Mail(),
        anti_abuse=_AntiAbuse(),
        human_verification=_HumanVerification(),
        on_retry=(lambda *args: retries.append(args)) if retries is not None else None,
    )


class StepRetryTest(unittest.TestCase):
    def test_transient_rpc_failure_is_retried(self):
        client = _FlakyClient(failures=2)
        retries: list = []
        result = _flow(client, retries=retries).run()

        self.assertTrue(result.success)
        self.assertEqual(3, client.attempts["create_email_code"])
        self.assertEqual(2, len(retries))
        self.assertEqual("create_email_code", retries[0][0])

    def test_non_retryable_error_fails_fast(self):
        client = _FlakyClient(failures=1, error=ValueError("email already registered"))
        with self.assertRaises(ValueError):
            _flow(client).run()
        self.assertEqual(1, client.attempts["create_email_code"])

    def test_retry_budget_is_respected(self):
        client = _FlakyClient(failures=5)
        with self.assertRaises(ProtocolError):
            _flow(client, attempts=2).run()
        self.assertEqual(2, client.attempts["create_email_code"])


class RetryClassificationTest(unittest.TestCase):
    def test_retryable(self):
        self.assertTrue(is_retryable_error(TimeoutError("slow")))
        self.assertTrue(is_retryable_error(ProtocolError("RPC transport failed after 3 attempts: x")))
        self.assertTrue(is_retryable_error(ProtocolError("gRPC status 14: unavailable")))
        self.assertTrue(is_retryable_error(RuntimeError("HTTP 502 Bad Gateway")))

    def test_not_retryable(self):
        self.assertFalse(is_retryable_error(ValueError("invalid sitekey")))
        self.assertFalse(is_retryable_error(ProtocolError("gRPC status 3: invalid argument")))


class ProfileDeFingerprintTest(unittest.TestCase):
    def test_password_has_no_fixed_affixes(self):
        """缺陷 11：原实现固定 "N!" 前缀 + "#7" 后缀，可被一条正则一网打尽。

        注意断言口径：修复后前后缀是随机的，偶发命中 "N!"/"#7" 属正常概率事件
        （实测各约 0.02%，即 1/5776 量级），因此只能断言"不是恒定模式"，
        不能断言"永不出现"——后者是 flaky 断言。
        """
        samples = [generate_password() for _ in range(400)]
        self.assertGreater(len(set(samples)), 380)
        # 决定性证据：首两位必须高度分散（原实现恒定只有 1 种）
        self.assertGreater(len({v[:2] for v in samples}), 100)
        # 统计性证据：命中率远低于 10%，不可能是固定前后缀
        self.assertLess(sum(v.startswith("N!") for v in samples), len(samples) // 10)
        self.assertLess(sum(v.endswith("#7") for v in samples), len(samples) // 10)
        for value in samples:
            self.assertGreaterEqual(len(value), PASSWORD_MIN_LENGTH)
            self.assertLessEqual(len(value), PASSWORD_MAX_LENGTH)

    def test_password_lengths_vary(self):
        lengths = {len(generate_password()) for _ in range(40)}
        self.assertGreater(len(lengths), 1)

    def test_name_space_is_much_larger_than_64(self):
        """缺陷 12：原实现只有 8x8=64 种组合。"""
        self.assertGreaterEqual(len(GIVEN_NAME_POOL) * len(FAMILY_NAME_POOL), 10_000)

    def test_profile_uses_known_pools(self):
        for _ in range(20):
            profile = generate_profile()
            self.assertTrue(profile.given_name and profile.family_name)
            self.assertTrue(profile.password)


if __name__ == "__main__":
    unittest.main()
