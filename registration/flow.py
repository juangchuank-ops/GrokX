#!/usr/bin/env python3
"""Browser-free registration coordinator built on HTTP, mail APIs, and gRPC-Web."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import secrets
import string
import threading
import time
from typing import Any, Callable, Protocol

from registration.protocol_client import (
    AuthProtocolClient,
    RpcResult,
    parse_message,
)
from providers.turnstile_flow import ChallengeContext


class MailProvider(Protocol):
    def create(self) -> tuple[str, str]: ...
    def wait_code(self, token: str, email: str) -> str: ...


class AntiAbuseProvider(Protocol):
    def acquire(self, *, stage: str, email: str) -> str: ...


class HumanVerificationProvider(Protocol):
    def acquire(self, challenge: ChallengeContext): ...


@dataclass(frozen=True)
class ProtocolRegistrationConfig:
    page_url: str
    sitekey: str
    action: str = ""
    tos_accepted_version: int = 1
    # 步骤级重试（指数退避），用于吸收收信延迟、RPC 偶发 429/502 等抖动
    step_attempts: int = 3
    step_backoff: float = 1.5
    max_step_backoff: float = 8.0


@dataclass(frozen=True)
class Profile:
    given_name: str
    family_name: str
    password: str


# 姓名池：足够大的组合空间，避免批量注册出现密集重名（原实现仅 8x8=64 种）
GIVEN_NAME_POOL = (
    "Aaron", "Adam", "Adrian", "Alan", "Albert", "Alex", "Alexander", "Andrew",
    "Anthony", "Arthur", "Austin", "Benjamin", "Blake", "Brandon", "Brian", "Bruce",
    "Caleb", "Cameron", "Carl", "Charles", "Christian", "Christopher", "Cody", "Colin",
    "Connor", "Daniel", "David", "Dennis", "Derek", "Dominic", "Dylan", "Edward",
    "Elias", "Eric", "Ethan", "Evan", "Felix", "Frank", "Gabriel", "Gavin",
    "George", "Gordon", "Grant", "Gregory", "Harold", "Henry", "Ian", "Isaac",
    "Jack", "Jacob", "James", "Jason", "Jeffrey", "Jeremy", "Jesse", "Joel",
    "John", "Jonathan", "Jordan", "Joseph", "Joshua", "Julian", "Justin", "Keith",
    "Kenneth", "Kevin", "Kyle", "Lawrence", "Leo", "Leonard", "Lewis", "Liam",
    "Logan", "Louis", "Lucas", "Luke", "Marcus", "Mark", "Martin", "Mason",
    "Matthew", "Maxwell", "Michael", "Miles", "Nathan", "Nicholas", "Noah", "Norman",
    "Oliver", "Oscar", "Owen", "Patrick", "Paul", "Peter", "Philip", "Preston",
    "Ralph", "Raymond", "Richard", "Robert", "Roger", "Ronald", "Ross", "Roy",
    "Russell", "Ryan", "Samuel", "Scott", "Sean", "Sebastian", "Simon", "Spencer",
    "Stanley", "Stephen", "Steven", "Stuart", "Terry", "Theodore", "Thomas", "Timothy",
    "Toby", "Travis", "Trevor", "Tyler", "Victor", "Vincent", "Walter", "Warren",
    "Wayne", "Wesley", "William", "Zachary",
)
FAMILY_NAME_POOL = (
    "Adams", "Alvarez", "Anderson", "Bailey", "Baker", "Barnes", "Bell", "Bennett",
    "Bishop", "Black", "Boyd", "Bradley", "Brooks", "Bryant", "Burke", "Burns",
    "Butler", "Campbell", "Carpenter", "Carroll", "Carter", "Chapman", "Clark", "Cole",
    "Coleman", "Collins", "Cooper", "Cox", "Craig", "Crawford", "Cunningham", "Curtis",
    "Davis", "Day", "Dean", "Dixon", "Douglas", "Duncan", "Dunn", "Ellis",
    "Evans", "Ferguson", "Fisher", "Fleming", "Ford", "Foster", "Fox", "Freeman",
    "Gardner", "Gibson", "Gilbert", "Gomez", "Graham", "Grant", "Gray", "Green",
    "Griffin", "Hall", "Hamilton", "Hansen", "Harper", "Harris", "Harrison", "Hart",
    "Hayes", "Henderson", "Henry", "Hernandez", "Hicks", "Hill", "Holmes", "Howard",
    "Hughes", "Hunt", "Hunter", "Jackson", "Jenkins", "Jensen", "Johnston", "Jordan",
    "Keller", "Kelly", "Kennedy", "Knight", "Lane", "Larson", "Lawrence", "Lawson",
    "Lee", "Lewis", "Long", "Lopez", "Marshall", "Martin", "Mason", "Matthews",
    "McCarthy", "McDonald", "Mendez", "Miller", "Mitchell", "Moore", "Morgan", "Morris",
    "Morrison", "Murphy", "Murray", "Nelson", "Newman", "Nichols", "Olson", "Ortiz",
    "Palmer", "Parker", "Parsons", "Patterson", "Payne", "Pearson", "Perez", "Perry",
    "Peterson", "Phillips", "Pierce", "Porter", "Powell", "Price", "Quinn", "Ramos",
    "Reed", "Reeves", "Reynolds", "Rice", "Richards", "Richardson", "Riley", "Rivera",
    "Roberts", "Robertson", "Robinson", "Rodriguez", "Rogers", "Ross", "Russell", "Ryan",
    "Sanchez", "Sanders", "Schmidt", "Schwartz", "Scott", "Shaw", "Simpson", "Sims",
    "Smith", "Snyder", "Spencer", "Stanley", "Stevens", "Stewart", "Stone", "Sullivan",
    "Sutton", "Taylor", "Terry", "Thompson", "Tucker", "Turner", "Wagner", "Walker",
    "Wallace", "Walsh", "Ward", "Warren", "Watson", "Weaver", "Webb", "Weber",
    "Wells", "West", "Wheeler", "White", "Whitney", "Williams", "Williamson", "Willis",
    "Wilson", "Wood", "Woods", "Wright", "Young",
)
# 密码字符集：不含引号与反斜杠，避免日志/配置转义问题
PASSWORD_SYMBOLS = "!@#$%^&*_-+=?"
PASSWORD_MIN_LENGTH = 16
PASSWORD_MAX_LENGTH = 22

_RETRYABLE_GRPC_STATUS = {"8", "10", "13", "14"}  # RESOURCE_EXHAUSTED/ABORTED/INTERNAL/UNAVAILABLE
_RETRYABLE_HTTP_STATUS = {"429", "500", "502", "503", "504"}
_faker_state = threading.local()


def _faker_person() -> tuple[str, str] | None:
    """优先用 Faker 生成自然人名；未安装时回落到内置姓名池。"""
    try:
        from faker import Faker  # type: ignore
    except Exception:
        return None
    try:
        instance = getattr(_faker_state, "instance", None)
        if instance is None:
            instance = Faker("en_US")
            _faker_state.instance = instance
        return str(instance.first_name()), str(instance.last_name())
    except Exception:
        return None


def generate_password(length: int | None = None) -> str:
    """高熵随机密码：无固定前后缀，长度与字符分布均随机。"""
    size = int(length or secrets.randbelow(PASSWORD_MAX_LENGTH - PASSWORD_MIN_LENGTH + 1) + PASSWORD_MIN_LENGTH)
    alphabet = string.ascii_letters + string.digits + PASSWORD_SYMBOLS
    pools = (string.ascii_lowercase, string.ascii_uppercase, string.digits, PASSWORD_SYMBOLS)
    chars = [secrets.choice(pool) for pool in pools]
    chars += [secrets.choice(alphabet) for _ in range(max(0, size - len(chars)))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def generate_profile() -> Profile:
    person = _faker_person()
    if person and person[0] and person[1]:
        given_name, family_name = person
    else:
        given_name = secrets.choice(GIVEN_NAME_POOL)
        family_name = secrets.choice(FAMILY_NAME_POOL)
    return Profile(given_name, family_name, generate_password())


def is_retryable_error(exc: BaseException) -> bool:
    """判断异常是否属于"重试有意义"的瞬时故障。"""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    text = str(exc)
    if "transport failed" in text:
        return True
    match = re.search(r"gRPC status (\d+)", text)
    if match and match.group(1) in _RETRYABLE_GRPC_STATUS:
        return True
    for status in _RETRYABLE_HTTP_STATUS:
        if re.search(rf"\b{status}\b", text):
            return True
    return False


@dataclass
class ProtocolRegistrationResult:
    success: bool
    state: str
    history: list[str] = field(default_factory=list)
    email: str = ""
    password: str = ""
    session_token: str = ""
    rpc: RpcResult | None = field(default=None, repr=False)


def _cookie_token(cookie_source: Any) -> str:
    if cookie_source is None:
        return ""
    for name in ("sso", "sso-rw"):
        try:
            value = cookie_source.get(name)
        except Exception:
            value = ""
        if value:
            return str(value)
    jar = getattr(cookie_source, "jar", cookie_source)
    try:
        iterable = list(jar)
    except Exception:
        iterable = []
    for cookie in iterable:
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        if name in {"sso", "sso-rw"} and value:
            return value
    return ""


def session_token_from_rpc(result: Any) -> str:
    for jar in (
        getattr(getattr(result, "response", None), "cookies", None),
        getattr(getattr(result, "session", None), "cookies", None),
    ):
        token = _cookie_token(jar)
        if token:
            return token
    try:
        for name in ("sso", "sso-rw"):
            value = result.response.cookies.get(name)
            if value:
                return str(value)
    except Exception:
        pass
    messages = getattr(result, "messages", None) or []
    if not messages:
        return ""
    try:
        outer = {field.number: field.value for field in parse_message(messages[0])}
        # CreateSessionV2Response.session -> CreateSessionResponse.session_cookie.
        nested = outer.get(1)
        if isinstance(nested, bytes):
            inner = {field.number: field.value for field in parse_message(nested)}
            token = inner.get(2)
            if isinstance(token, bytes):
                return token.decode("utf-8", "replace")
    except Exception:
        return ""
    return ""


class ProtocolRegistrationFlow:
    """Execute the full registration state machine without a browser object."""

    def __init__(
        self,
        *,
        config: ProtocolRegistrationConfig,
        client: AuthProtocolClient,
        mail: MailProvider,
        anti_abuse: AntiAbuseProvider,
        human_verification: HumanVerificationProvider,
        on_progress: Callable[[str], None] | None = None,
        on_retry: Callable[[str, int, float, str], None] | None = None,
    ):
        self.config = config
        self.client = client
        self.mail = mail
        self.anti_abuse = anti_abuse
        self.human_verification = human_verification
        self.on_progress = on_progress
        self.on_retry = on_retry

    def _progress(self, stage: str, history: list[str]) -> None:
        history.append(stage)
        if self.on_progress is not None:
            self.on_progress(stage)

    def _retry(
        self,
        label: str,
        action: Callable[[], Any],
        *,
        retry_on: Callable[[BaseException], bool] | None = None,
        attempts: int | None = None,
    ) -> Any:
        """步骤级指数退避重试：单步抖动不再让整条链路作废。"""
        total = max(1, int(attempts if attempts is not None else self.config.step_attempts))
        predicate = retry_on or is_retryable_error
        last_error: BaseException | None = None
        for attempt in range(1, total + 1):
            try:
                return action()
            except Exception as exc:
                last_error = exc
                if attempt >= total or not predicate(exc):
                    raise
                delay = min(
                    self.config.step_backoff * (2 ** (attempt - 1)),
                    self.config.max_step_backoff,
                )
                if self.on_retry is not None:
                    self.on_retry(label, attempt, delay, f"{type(exc).__name__}: {exc}")
                time.sleep(delay)
        raise last_error if last_error else RuntimeError(f"{label} 重试失败")

    def run(self) -> ProtocolRegistrationResult:
        history: list[str] = []
        self._progress("init", history)
        self._retry("bootstrap", lambda: self.client.bootstrap(self.config.page_url))
        self._progress("protocol_session_bootstrapped", history)

        email, mail_token = self._retry("create_mailbox", self.mail.create)
        self._progress("email_created", history)

        email_castle_token = self._retry(
            "email_anti_abuse",
            lambda: self.anti_abuse.acquire(stage="email", email=email),
        )
        self._progress("email_anti_abuse_token_ready", history)
        self._retry(
            "create_email_code",
            lambda: self.client.create_email_validation_code(
                email,
                castle_request_token=email_castle_token,
            ),
        )
        self._progress("email_code_requested", history)

        # 收信延迟是常态，允许更长的重试预算
        code = str(
            self._retry(
                "wait_email_code",
                lambda: self.mail.wait_code(mail_token, email),
                retry_on=lambda _exc: True,
                attempts=max(self.config.step_attempts, 4),
            )
            or ""
        ).replace("-", "").strip()
        if not code:
            raise RuntimeError("mail provider returned an empty verification code")
        self._progress("email_code_received", history)

        self._retry("verify_email_code", lambda: self.client.verify_email_validation_code(email, code))
        self._progress("email_code_verified", history)

        profile = generate_profile()

        challenge = ChallengeContext(
            page_url=self.config.page_url,
            sitekey=self.config.sitekey,
            action=self.config.action,
        )

        def _acquire_turnstile() -> str:
            acquired = self.human_verification.acquire(challenge)
            token = str(getattr(acquired, "value", acquired) or "").strip()
            if not token:
                raise RuntimeError("human verification provider returned an empty token")
            return token

        turnstile_token = self._retry("turnstile", _acquire_turnstile)
        self._progress("turnstile_token_ready", history)

        final_castle_token = self._retry(
            "final_anti_abuse",
            lambda: self.anti_abuse.acquire(stage="final", email=email),
        )
        self._progress("final_anti_abuse_token_ready", history)
        result = self._retry(
            "create_session",
            lambda: self.client.create_user_and_session(
                email=email,
                given_name=profile.given_name,
                family_name=profile.family_name,
                password=profile.password,
                email_validation_code=code,
                turnstile_token=turnstile_token,
                castle_request_token=final_castle_token,
                conversion_id=secrets.token_hex(16),
                tos_accepted_version=self.config.tos_accepted_version,
                use_v2=True,
            ),
        )
        self._progress("create_session_rpc_completed", history)
        session_token = session_token_from_rpc(result)
        if not session_token:
            raise RuntimeError("create-session response contained no session token")
        self._progress("session_token_ready", history)
        return ProtocolRegistrationResult(
            True,
            "completed",
            history,
            email=email,
            password=profile.password,
            session_token=session_token,
            rpc=result,
        )
