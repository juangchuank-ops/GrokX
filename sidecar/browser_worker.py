#!/usr/bin/env python3
"""sidecar/browser_worker.py: Playwright 常驻无头浏览器引擎。

设计要点（相对原始方案蓝图的加固）：
1. **单线程亲和**：Playwright 同步 API 的事件循环绑定在创建它的线程上，
   任何跨线程调用都会抛 ``greenlet.error: Cannot switch to a different thread``。
   而缓冲池的补水线程与业务线程都需要产出 Token，因此本模块把**所有**浏览器
   操作投递到一个专用「浏览器线程」的命令队列里串行执行，对外仍暴露同步方法。
2. **代理闭环**：浏览器实例挂载与 gRPC-Web 完全相同的代理，消除 IP 漂移。
3. **真实指纹**：使用真实 Chromium 渲染管线（真实 Canvas/WebGL/Audio 熵），
   仅抹除 webdriver 等自动化标志，不伪造硬件指纹（伪造反而降低可信度）。
4. **惰性依赖**：playwright 未安装时给出可执行的报错，而不是 ImportError 崩溃。
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import queue
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlsplit

HARNESS_URL = "https://accounts.x.ai/__turnstile_harness__"
HARNESS_PATH = Path(__file__).parent / "harness.html"
HARNESS_ROUTE_PATTERN = re.compile(r"^https://accounts\.x\.ai/__turnstile_harness__")
DEFAULT_CONTAINER = "#cf-turnstile-widget"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
CHALLENGE_HOST_HINTS = ("challenges.cloudflare.com", "turnstile")
CHECKBOX_SELECTORS = (
    "input[type='checkbox']",
    ".ctp-checkbox-container",
    "#challenge-stage",
)
INTERACTIVE_SELECTORS = (".ctp-image-grid", "#challenge-success", ".ctp-opacity-grid")
SHUTDOWN_TIMEOUT = 10.0
# Cloudflare 官方 "always passes" 测试 sitekey：不做风险评估，用于把
# 「Sidecar 自身故障」与「出口 IP / 环境被风控拒绝」区分开
TURNSTILE_TEST_SITEKEY = "1x00000000000000000000AA"
# 自检专用容器：Turnstile 禁止同一容器在 render/execute 之间更换 sitekey
SELFTEST_CONTAINER = "#cf-turnstile-selftest"

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || {
  runtime: {},
  app: { isInstalled: false, InstallState: { DISABLED: 'disabled' }, RunningState: { RUNNING: 'running' } },
  csi: function () {},
  loadTimes: function () {},
};
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
  ],
});
if (navigator.permissions && navigator.permissions.query) {
  const originalQuery = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (parameters) =>
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: typeof Notification !== 'undefined' ? Notification.permission : 'default' })
      : originalQuery(parameters);
}
"""


class SidecarError(RuntimeError):
    """Sidecar 运行期错误基类。"""


class SidecarUnavailable(SidecarError):
    """Playwright / Chromium 内核不可用，调用方应降级到外部 Provider。"""


class TurnstileChallengeError(SidecarError):
    """Turnstile 返回了错误码。"""


class InteractiveChallengeError(SidecarError):
    """降级为需要人工选图的交互式挑战，通常意味着代理 IP 信誉恶化。"""


class BrowserWorkerTimeout(SidecarError):
    """求解超时。"""


@dataclass(frozen=True)
class TokenPair:
    turnstile_token: str
    castle_token: str
    created_at: float


def playwright_proxy_kwargs(proxy_url: str) -> dict[str, Any]:
    """把项目归一化的代理 URL 转成 Playwright 可用的 proxy 参数。

    - Playwright/Chromium 不认 ``socks5h``，需要回落为 ``socks5``（Chromium 本身
      始终在代理端做远程 DNS 解析，语义与 socks5h 等价）。
    - Playwright 建议把认证信息放在独立字段，而不是内联在 server URL 里。
    """
    value = str(proxy_url or "").strip()
    if not value:
        return {}
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"无法解析的代理地址: {value}")
    scheme = parts.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in {"http", "https", "socks4", "socks5"}:
        raise ValueError(f"Playwright 不支持的代理协议: {scheme}")
    host = parts.hostname
    if ":" in host:  # IPv6
        host = f"[{host}]"
    server = f"{scheme}://{host}:{parts.port}" if parts.port else f"{scheme}://{host}"
    kwargs: dict[str, Any] = {"server": server}
    if parts.username:
        kwargs["username"] = unquote(parts.username)
    if parts.password:
        kwargs["password"] = unquote(parts.password)
    return kwargs


class _Command:
    __slots__ = ("fn", "future")

    def __init__(self, fn: Callable[[], Any]):
        self.fn = fn
        self.future: Future = Future()


class BrowserWorker:
    """常驻无头浏览器实例：负责 Harness 路由劫持、Token 生产与环境自愈。

    对外是同步 API；内部所有 Playwright 调用都被投递到专用浏览器线程执行，
    因此可以从任意线程（缓冲池补水线程、并发业务线程）安全调用。
    """

    def __init__(
        self,
        *,
        proxy: str = "",
        user_agent: str = "",
        sitekey: str = "0x4AAAAAAAhr9JGVDZbrZOo0",
        castle_pk: str = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz",
        headless: bool = True,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
        viewport_width: int = 1280,
        viewport_height: int = 800,
        browser_channel: str = "",
        start_timeout: float = 45.0,
        harness_path: Optional[Path] = None,
    ):
        self.proxy = str(proxy or "").strip()
        self.user_agent = str(user_agent or "").strip() or DEFAULT_USER_AGENT
        self.sitekey = str(sitekey or "").strip()
        self.castle_pk = str(castle_pk or "").strip()
        self.headless = bool(headless)
        self.locale = str(locale or "en-US")
        self.timezone_id = str(timezone_id or "America/New_York")
        self.viewport = {"width": int(viewport_width), "height": int(viewport_height)}
        self.browser_channel = str(browser_channel or "").strip()
        self.start_timeout = float(start_timeout)

        path = Path(harness_path) if harness_path else HARNESS_PATH
        self._html_content = path.read_text(encoding="utf-8")

        self._commands: "queue.Queue[Optional[_Command]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._started_event = threading.Event()
        self._state_lock = threading.Lock()
        self._start_error: Optional[BaseException] = None
        self._started = False
        self._closed = False
        self._tokens_produced = 0
        self._last_error = ""

        # 仅浏览器线程内部使用
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # ------------------------------------------------------------------ 状态

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def tokens_produced(self) -> int:
        return self._tokens_produced

    @property
    def last_error(self) -> str:
        return self._last_error

    def _owner_thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ 命令通道

    def _submit(self, fn: Callable[[], Any], timeout: float) -> Any:
        """把一段浏览器操作投递到浏览器线程执行，并同步等待结果。"""
        if threading.current_thread() is self._thread:
            return fn()
        if not self._owner_thread_alive():
            raise SidecarError("浏览器线程未运行，Sidecar 不可用")
        command = _Command(fn)
        self._commands.put(command)
        return command.future.result(timeout=timeout)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        with self._state_lock:
            if self.started:
                return
            if self._closed:
                raise SidecarError("BrowserWorker 已关闭，无法复用")
            if self._owner_thread_alive():
                # 线程已存在但初始化失败过，直接复用其错误
                if self._start_error is not None:
                    raise self._start_error
                self._started_event.wait(self.start_timeout)
                if self._start_error is not None:
                    raise self._start_error
                return
            self._start_error = None
            self._started_event.clear()
            self._thread = threading.Thread(target=self._thread_main, name="sidecar-browser", daemon=True)
            self._thread.start()

        if not self._started_event.wait(self.start_timeout):
            raise SidecarUnavailable(f"浏览器初始化超过 {self.start_timeout:g}s 未完成")
        if self._start_error is not None:
            raise self._start_error

    def _thread_main(self) -> None:
        try:
            self._launch()
            self._new_context()
        except BaseException as exc:  # 初始化失败：让 start() 抛出，并拒绝后续命令
            self._start_error = exc
            self._started_event.set()
            self._drain_commands(exc)
            self._teardown_browser()
            return
        self._started = True
        self._started_event.set()
        try:
            while True:
                command = self._commands.get()
                if command is None:
                    break
                try:
                    command.future.set_result(command.fn())
                except BaseException as exc:  # noqa: BLE001 - 需要原样回传给调用方
                    command.future.set_exception(exc)
        finally:
            self._started = False
            self._teardown_browser()

    def _drain_commands(self, error: BaseException) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if command is None:
                continue
            command.future.set_exception(error)

    def _teardown_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        self._context = None
        self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def close(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        with self._state_lock:
            if self._closed:
                return
            thread = self._thread
            self._closed = True
        if thread is not None and thread.is_alive():
            self._commands.put(None)
            thread.join(timeout=timeout)
        self._thread = None
        self._started = False

    def __enter__(self) -> "BrowserWorker":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _ensure_started(self) -> None:
        if not self.started:
            self.start()

    # ------------------------------------------------------------------ 浏览器线程内部

    @staticmethod
    def _import_playwright():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise SidecarUnavailable(
                "本地 Sidecar 需要 Playwright：pip install 'playwright>=1.40' "
                "（复用系统已安装的 Chrome/Edge，无需 playwright install 下载内核）"
            ) from exc
        return sync_playwright

    def _launch_args(self) -> list[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            # 无头模式下保留 WebGL：SwiftShader 软渲染，避免 "No available adapters"
            "--enable-unsafe-swiftshader",
            f"--window-size={self.viewport['width']},{self.viewport['height']}",
            f"--lang={self.locale}",
        ]

    def _launch(self) -> None:
        sync_playwright = self._import_playwright()
        self._playwright = sync_playwright().start()
        proxy_kwargs = playwright_proxy_kwargs(self.proxy)
        channels = [self.browser_channel] if self.browser_channel else ["chrome", "msedge", ""]
        errors: list[str] = []
        for channel in channels:
            kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": self._launch_args(),
            }
            if channel:
                kwargs["channel"] = channel
            if proxy_kwargs:
                kwargs["proxy"] = proxy_kwargs
            try:
                self._browser = self._playwright.chromium.launch(**kwargs)
                self._last_error = ""
                return
            except Exception as exc:  # 逐个渠道降级尝试
                errors.append(f"{channel or 'bundled-chromium'}: {exc}")
        raise SidecarUnavailable(
            "未能启动任何 Chromium 内核（请确认已安装 Chrome 或 Edge，或执行 "
            "playwright install chromium）。尝试结果: " + " | ".join(errors)[:500]
        )

    def _new_context(self) -> None:
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            viewport=self.viewport,
            locale=self.locale,
            timezone_id=self.timezone_id,
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            java_script_enabled=True,
            color_scheme="dark",
        )
        self._context.set_default_timeout(self.start_timeout * 1000)
        self._context.add_init_script(STEALTH_INIT_SCRIPT)
        self._page = self._context.new_page()

        def _route_handler(route: Any) -> None:
            try:
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=self._html_content,
                )
            except Exception:  # pragma: no cover - 竞态关闭时忽略
                pass

        self._page.route(HARNESS_ROUTE_PATTERN, _route_handler)
        self._page.goto(HARNESS_URL, wait_until="domcontentloaded", timeout=self.start_timeout * 1000)
        self._wait_for_sdks()

    def _wait_for_sdks(self) -> None:
        """等待 Turnstile 与 Castle 脚本就绪；缺失只记录，不致命（可能被代理拦截）。"""
        try:
            self._page.wait_for_function(
                "() => typeof window.turnstile !== 'undefined'",
                timeout=min(self.start_timeout, 20.0) * 1000,
            )
            # render=explicit 下必须等 ready 回调，否则 render() 会静默失败
            state = self._page.evaluate(
                "() => new Promise((resolve) => {"
                "  let settled = false;"
                "  const done = (value) => { if (!settled) { settled = true; resolve(value); } };"
                "  try { window.turnstile.ready(() => done('ready')); }"
                "  catch (err) { done('error: ' + String((err && err.message) || err)); }"
                "  setTimeout(() => done('timeout'), 10000);"
                "})"
            )
            if state != "ready":
                self._last_error = f"turnstile not ready: {state}"
        except Exception as exc:
            self._last_error = f"turnstile sdk not ready: {exc}"
        try:
            self._page.wait_for_function(
                "() => typeof window._castle !== 'undefined'",
                timeout=min(self.start_timeout, 20.0) * 1000,
            )
            if self.castle_pk:
                self._page.evaluate("(pk) => initCastle(pk)", self.castle_pk)
        except Exception as exc:
            self._last_error = f"castle sdk not ready: {exc}"

    # ------------------------------------------------------------------ 对外操作

    def restart_context(self, timeout: float = 60.0) -> None:
        """软重启：仅重建 Context/Page，保留浏览器进程，用于清理内存膨胀。"""
        self._ensure_started()
        self._submit(self._restart_context_inner, timeout)

    def _restart_context_inner(self) -> None:
        old_context = self._context
        self._context = None
        self._page = None
        if old_context is not None:
            try:
                old_context.close()
            except Exception:
                pass
        self._new_context()

    def status(self, timeout: float = 30.0) -> dict[str, Any]:
        """返回 Harness 页面的真实环境快照，便于排查风控问题。"""
        self._ensure_started()
        try:
            snapshot = self._submit(lambda: self._page.evaluate("() => harnessStatus()"), timeout)
        except Exception as exc:
            raise SidecarError(f"Harness 环境自检失败: {exc}") from exc
        return {
            "proxy": bool(self.proxy),
            "tokens_produced": self._tokens_produced,
            "last_error": self._last_error,
            **(snapshot or {}),
        }

    def produce_turnstile_token(
        self,
        action: str = "",
        timeout: float = 25.0,
        sitekey: str = "",
        container: str = "",
    ) -> str:
        self._ensure_started()
        token = self._submit(
            lambda: self._produce_turnstile_inner(action, timeout, sitekey, container),
            timeout + 10.0,
        )
        return token

    def _produce_turnstile_inner(
        self,
        action: str,
        timeout: float,
        sitekey: str = "",
        container: str = "",
    ) -> str:
        target_sitekey = str(sitekey or "").strip() or self.sitekey
        target_container = str(container or "").strip() or DEFAULT_CONTAINER
        rendered = self._page.evaluate(
            "([sitekey, action, container]) => renderTurnstile(sitekey, action || undefined, container || undefined)",
            [target_sitekey, str(action or "").strip(), target_container],
        )
        if not rendered:
            error = self._page.evaluate("(sel) => window._turnstileErrors[sel] || window._turnstileError", target_container)
            raise TurnstileChallengeError(f"Turnstile 渲染失败: {error or 'UNKNOWN'}")

        started = time.monotonic()
        deadline = started + max(1.0, float(timeout))
        last_click = 0.0
        while time.monotonic() < deadline:
            state = self._page.evaluate(
                "(sel) => ({token: window._turnstileTokens[sel], error: window._turnstileErrors[sel],"
                " frames: document.querySelectorAll('iframe').length})",
                target_container,
            )
            token = (state or {}).get("token")
            if token:
                self._tokens_produced += 1
                return str(token)
            error = (state or {}).get("error")
            if error:
                raise TurnstileChallengeError(f"Turnstile 挑战失败: {error}")
            # 静默失败兜底：widget 迟迟不创建 iframe，说明 Cloudflare 未真正下发挑战。
            # 两种典型原因：(1) api.js 带了 async/defer；(2) 真实 sitekey 的风险评估
            # 判定当前出口 IP/环境不可信，直接 no-op（用官方测试 key 可区分）。
            if not (state or {}).get("frames") and time.monotonic() - started > 12.0:
                raise TurnstileChallengeError(
                    "Turnstile 未创建挑战 iframe（widget 静默失败）：请确认 harness.html 中 "
                    "api.js 未使用 async/defer；若使用真实 sitekey，通常是当前出口 IP 信誉不足，"
                    "需配置住宅代理（可用 --sidecar-check --sidecar-produce 的 self_test 字段区分）"
                )
            now = time.monotonic()
            if now - last_click > 1.5:
                last_click = now
                self._click_challenge()
            time.sleep(0.25)
        raise BrowserWorkerTimeout(f"Turnstile 求解在 {timeout:g}s 内超时")

    def produce_castle_token(self, timeout: float = 20.0) -> str:
        self._ensure_started()
        # 浏览器线程可能正被补水任务占用，这里留足排队余量，避免误报为空错误
        return self._submit(lambda: self._produce_castle_inner(timeout), timeout + 60.0)

    def _produce_castle_inner(self, timeout: float = 20.0) -> str:
        if not self.castle_pk:
            raise SidecarError("Castle publishable key 为空")
        try:
            token = self._page.evaluate(
                "(ms) => acquireCastleToken(ms)",
                int(max(1.0, float(timeout)) * 1000),
            )
        except Exception as exc:
            raise SidecarError(f"Castle SDK 未返回有效 Token: {exc}") from exc
        if not token:
            raise SidecarError("Castle SDK 未返回有效 Token")
        return str(token)

    def produce_token_pair(self, action: str = "", timeout: float = 25.0) -> TokenPair:
        """一次性产出 Turnstile + Castle 组合，供缓冲池批量补水。"""
        turnstile = self.produce_turnstile_token(action, timeout=timeout)
        castle = self.produce_castle_token()
        return TokenPair(turnstile, castle, time.time())

    # ------------------------------------------------------------------ 交互式挑战

    def _challenge_frames(self) -> list[Any]:
        frames = []
        try:
            iterable = list(self._page.frames)
        except Exception:
            return frames
        for frame in iterable:
            url = str(getattr(frame, "url", "") or "")
            if any(hint in url for hint in CHALLENGE_HOST_HINTS):
                frames.append(frame)
        return frames

    def _click_challenge(self) -> bool:
        """Managed 交互模式：在挑战 iframe 内做拟人化点击。"""
        for frame in self._challenge_frames():
            for selector in CHECKBOX_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() == 0 or not locator.is_visible():
                        continue
                    box = locator.bounding_box()
                except Exception:
                    continue
                if not box:
                    continue
                self._human_click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                if self._has_interactive_grid(frame):
                    raise InteractiveChallengeError(
                        "Turnstile 已降级为图形选择挑战，当前代理 IP 信誉过低"
                    )
                return True
        return False

    @staticmethod
    def _has_interactive_grid(frame: Any) -> bool:
        for selector in INTERACTIVE_SELECTORS:
            try:
                if frame.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _human_click(self, x: float, y: float) -> None:
        """带随机抖动与中间路径的鼠标点击，避免 (x, y, 0ms) 的机械落点。"""
        try:
            start_x = x - random.uniform(60, 140)
            start_y = y - random.uniform(40, 90)
            self._page.mouse.move(start_x, start_y, steps=random.randint(4, 8))
            mid_steps = random.randint(3, 6)
            for index in range(1, mid_steps + 1):
                ratio = index / mid_steps
                jitter_x = x * ratio + (start_x * (1 - ratio)) + random.uniform(-3, 3)
                jitter_y = y * ratio + (start_y * (1 - ratio)) + random.uniform(-3, 3)
                self._page.mouse.move(jitter_x, jitter_y, steps=random.randint(2, 4))
                time.sleep(random.uniform(0.01, 0.05))
            self._page.mouse.move(x, y, steps=random.randint(2, 4))
            time.sleep(random.uniform(0.05, 0.15))
            self._page.mouse.down()
            time.sleep(random.uniform(0.06, 0.14))
            self._page.mouse.up()
        except Exception:
            try:
                self._page.mouse.click(x, y)
            except Exception:
                pass
