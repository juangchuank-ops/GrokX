# CHANGELOG

## v1.1.0 — 本地无头浏览器 Sidecar 与风控一致性改造

本次改动基于两份设计文档实施：`TURNSTILE_SIDECAR_SOLUTION.md`（Sidecar 方案蓝图）与 `PROJECT_ANALYSIS.md`（缺陷评估与重构路线）。核心目标是**消除 IP/指纹裂痕、去掉批量关联特征、补齐工程化短板**，同时保证原生链路（CapSolver + Node JSDOM）作为默认行为不被破坏。

---

## 一、新增：本地无头浏览器 Sidecar（`sidecar/`）

新增可选子系统，用**本机真实 Chromium** 产出 Turnstile 与 Castle 令牌，替代 CapSolver 与 Node JSDOM。

| 文件 | 职责 |
|------|------|
| `sidecar/harness.html` | 在 `accounts.x.ai` 域上下文下运行的轻量宿主页，加载官方 Turnstile 与 Castle SDK，并把 Token 回传给 Python |
| `sidecar/browser_worker.py` | Playwright 常驻实例：启动/渠道降级、路由劫持、Stealth 注入、Token 生产、拟人化点击、Context 软重启 |
| `sidecar/token_pool.py` | Turnstile 缓冲池：FIFO + TTL 淘汰 + 自适应补水 + 失败上报 + 自愈 |
| `sidecar/service.py` | 进程内单例门面：`acquire_turnstile` / `acquire_castle` / `status` / `stop`，并提供降级信号 |
| `providers/local_sidecar.py` | 适配 `HumanVerificationProvider` / `AntiAbuseProvider` 协议，零改动替换原 Provider |

### 1.1 突破 Cloudflare 域名白名单（Origin Spoofing）

Turnstile 初始化时会校验 `window.location.origin` 是否在域名白名单内，本地 `localhost` 会直接报错。实现方式是用 Playwright 路由拦截，把 `https://accounts.x.ai/__turnstile_harness__` 直接 fulfill 成本地 `harness.html`：

```python
self._page.route(HARNESS_ROUTE_PATTERN, _route_handler)   # 正则匹配，兼容 query string
self._page.goto(HARNESS_URL, wait_until="domcontentloaded")
```

浏览器地址栏与 DOM 上下文归属真实注册域，且**不产生任何真实网络请求**。

### 1.2 专用浏览器线程 + 命令队列（修掉方案蓝图里的一个致命坑）

Playwright 同步 API 的事件循环绑定在创建它的线程上。原蓝图里 TokenPool 的补水线程会直接调用 `worker.produce_turnstile_token()`，实机运行会抛：

```
greenlet.error: Cannot switch to a different thread
```

而且它**不会**让调用方拿到异常（错误发生在 asyncio 的 done-callback 里），表现为「Token 池永远空、却看不到报错」。本次把 `BrowserWorker` 改成**单线程亲和**模型：

- 所有浏览器操作都投递到一条名为 `sidecar-browser` 的专用线程的命令队列（`queue.Queue` + `Future`）串行执行；
- 对外仍是同步方法，可被补水线程与并发业务线程安全调用；
- 初始化失败会把异常**原样回抛**给 `start()`，让 CLI 能做降级判断；
- 关闭时投递哨兵并 join，`playwright.stop()` 在拥有者线程内执行。

### 1.3 Token 缓冲池

- FIFO 队列 + **240s TTL**（Turnstile 默认 300s，留 60s 安全冗余）；
- 后台补水线程按 `pool_size`（默认 2）自动补齐，业务侧出队**零延迟**；
- 连续 3 次失败 → 上报 `proxy_unhealthy` 事件并软重启 Context（提示轮换出口 IP）；
- 累计产出 100 个 Token → 自动重建 Context，控制 Chromium 长期驻留的内存膨胀；
- 池空且有超时压力时**降级为实时生成**，不直接失败。

### 1.4 代理闭环

- 浏览器实例挂载与 `PROXY` **完全相同的出口**，`socks5h` 自动转换为 Chromium 可用的 `socks5`（Chromium 本身在代理端做远程解析，语义等价），认证信息拆到 `username`/`password` 字段；
- 结果是「Turnstile 求解 / Castle 上报 / 发信 RPC / 注册 RPC」四者出口一致，消除跨 ASN 的会话 IP 漂移。

### 1.5 其他

- Stealth 注入只抹除自动化标志（`navigator.webdriver`、`window.chrome`、`plugins`、`permissions.query`），**不伪造** WebGL/Canvas 硬件指纹——真实渲染管线的熵本来就比伪造值更可信；
- 无头模式下加 `--enable-unsafe-swiftshader`，避免 WebGL 因无 GPU 而不可用；
- 启动渠道按 `chrome` → `msedge` → 内置 chromium 依次降级，复用系统已装浏览器，无需下载内核；
- Managed 交互式挑战时做带随机抖动与中间路径的拟人化点击；识别到图形选择网格时抛 `InteractiveChallengeError`，触发代理健康告警。

### 1.6 实机验证阶段暴露并修掉的 4 个问题

方案蓝图给出的示例代码在真实运行时会踩以下坑，本次全部修掉：

| # | 问题 | 现象 | 修复 |
|---|------|------|------|
| 1 | Turnstile 的 `api.js` 带 `async defer` | `render()` **静默失败**：不创建 iframe、不触发 error-callback、token 永不返回，表现为「无头浏览器启动了但永远拿不到 token」 | 去掉 `async defer`（官方明确要求 `render=explicit` 下不能带），并用 `turnstile.ready()` 确认就绪 |
| 2 | Castle `createRequestToken()` 的返回值是**自定义 thenable**（只有 `then`，没有 `catch`） | `.then(...).catch(...)` 直接抛 `catch is not a function`；且该 thenable 在异常环境下可能永不兑现，把 Python 侧挂死 | 改用 `then(onFulfilled, onRejected)` 两参形式 + JS 侧超时，并把 `SIDECAR_CASTLE_TIMEOUT` 透传到 worker |
| 3 | 自检与真实 widget 共用同一个容器 | Turnstile 报 `sitekey ... is/are not allowed be changed between the calls of render() and execute()`，自检永远失败 | 新增 `#cf-turnstile-selftest` 独立容器，token/error 按容器隔离存储（`_turnstileTokens[selector]`） |
| 4 | 缓冲池失败重试占满浏览器线程 | Castle 请求排队超时，异常信息为空（`TimeoutError` 无 message），难以定位 | 失败退避改为随失败次数递增（上限 30s）；Castle 请求预留排队余量；静默失败改为 12s 快速报错并给出可执行提示 |

同时补上**混合双轨容灾**（文档 §7.2）：即使 Sidecar 已启动，单个环节失败也会自动回退到备用 Provider（`FallbackTurnstileProvider` / `FallbackAntiAbuseProvider`，发出 `provider_fallback` 事件），而不是让整条链路失败。

### 1.7 自检能力：区分「代码故障」与「出口被风控」

`--sidecar-check --sidecar-produce` 现在会先用 Cloudflare 官方 always-pass 测试 key（`1x00000000000000000000AA`）跑一遍完整链路，再跑真实 sitekey：

- `self_test.ok = true` → Harness、路由劫持、无头环境、token 回传全部正常，**问题在代理出口**；
- `self_test.ok = false` → Sidecar 自身有问题（依赖缺失、SDK 加载失败等）。

这消除了「到底是我的环境不行，还是代码不行」的排查死循环。

---

## 二、修复功能缺陷（P0）

### 2.1 CapSolver 代理从未生效

`CapSolverProvider.__init__` 接收并保存了 `proxy`，但 `acquire()` 里硬编码 `AntiTurnstileTaskProxyLess`，`self.proxy` 被完全丢弃——打码 IP 与注册 IP 分属两个网络。

修复：新增 `_proxy_fields()` / `_task_payload()`，有代理时切到 `AntiTurnstileTask` 并传 `proxyType` / `proxyAddress` / `proxyPort` / `proxyLogin` / `proxyPassword`，同时把 `userAgent` 一并传入；代理地址缺端口或协议不支持时明确报错，不再静默退化成 ProxyLess。

### 2.2 其他修复

| 位置 | 问题 | 修复 |
|------|------|------|
| `registration/cli.py` | `--events` 帮助文本为 GBK/UTF-8 混用的乱码 | 恢复为「输出脱敏后的 JSONL 进度事件」 |
| `network/proxy.py` | 报错仍写「浏览器代理暂只支持 HTTP/HTTPS」 | 改为列出实际支持的协议族，去掉过时描述 |

---

## 三、风控与环境一致性

### 3.1 TLS 指纹与 UA 版本对齐

原实现随机取 UA 主版本 126~135，但 Session 固定 `impersonate="chrome"`（约等于 Chrome 120 的 ClientHello），Cloudflare 边缘会判定「指纹与 UA 不一致」。

修复：`network/fingerprint.py` 改为**先**从 `curl_cffi` 实际支持的 impersonate 目标（`chrome120/123/124/131/133a/136/142/145/146/150`…）里在版本窗口内挑选，**再由目标反推 UA 主版本**，两者天然同版本；`AuthProtocolClient` 新增 `impersonate` 参数并把它绑定到指纹，目标不被支持时回落到泛化 `chrome` 并同步更新内部状态。`FINGERPRINT_CHROME_MIN/MAX` 同步上调到 131~150。

### 3.2 `sec-ch-ua` 不再写死

原实现固定拼接 `"Not.A/Brand";v="24"`，是极明显的脚本特征。改为从 6 个真实 Chrome 出现过的 GREASE 占位串中随机选取，并**随机打乱三个品牌的顺序**（Chromium 本身就会打乱）。

### 3.3 `Accept-Language` 与出口区域对齐

默认从 `zh-CN,zh;q=0.9,en;q=0.8` 改为 `en-US,en;q=0.9`，并支持 `FINGERPRINT_REGION`（us/gb/de/fr/jp/kr/sg/hk/tw/cn 映射表）或 `ACCEPT_LANGUAGE` 显式覆盖，避免「挂美国住宅代理却声明中文偏好」。

### 3.4 前端公钥外部化

`PROTOCOL_TURNSTILE_SITEKEY`、`PROTOCOL_CASTLE_PUBLISHABLE_KEY` 改为优先读 `.env`（`PROTOCOL_TURNSTILE_SITEKEY` / `PROTOCOL_CASTLE_PUBLISHABLE_KEY`），缺省才用内置默认值——官方轮换公钥时无需改代码。

---

## 四、防关联治理

| 项 | 原实现 | 现实现 |
|----|--------|--------|
| 密码 | 固定 `"N!" + 18 位 + "#7"`，长度恒为 22，一条正则即可全网筛查 | 长度 16~22 随机，四类字符集必含，`SystemRandom` 洗牌，无固定前后缀 |
| 姓名 | 8×8 = 64 种组合 | 内置 130+ 名字 / 200+ 姓氏池（组合空间 > 26000），并在装了 `faker` 时优先用 `Faker("en_US")` 生成自然人名（按线程缓存实例） |

---

## 五、健壮性

### 5.1 状态机步骤级重试

`ProtocolRegistrationFlow` 每个阶段都包一层指数退避重试（`STEP_ATTEMPTS` 默认 3、`STEP_BACKOFF` 默认 1.5s、上限 `MAX_STEP_BACKOFF` 默认 8s），只对**瞬时故障**重试：超时/连接错误、`RPC transport failed`、gRPC 状态 8/10/13/14（RESOURCE_EXHAUSTED/ABORTED/INTERNAL/UNAVAILABLE）、HTTP 429/500/502/503/504。收信阶段单独放宽重试预算。业务错误（如「邮箱已注册」）立即失败，不做无谓重试。新增 `on_retry` 回调，CLI 会输出 `retry` 事件。

### 5.2 Protobuf 解码扩展性

`parse_message` 增加 WireType 1（fixed64）与 WireType 5（fixed32）支持，不再因为未知 wire type 直接抛 `ProtocolError`。

### 5.3 Sidecar 失败平滑降级

CLI 启动 Sidecar 失败（缺 Playwright、无 Chromium 内核）时打印原因并自动回退到 CapSolver + Node SDK；若此时 `CAPSOLVER_API_KEY` 也缺失，才以配置错误退出（退出码 2）。

---

## 六、工程化

- **测试**：从 2 个文件 4 个用例扩到 **7 个文件 75 个用例**，新增覆盖 `protocol_client`（varint/字段编码/gRPC-Web 帧拆装/WireType 1、5/`CreateUserAndSessionV2` 嵌套字段布局回放）、指纹一致性、CapSolver 代理透传、代理归一化、Token 缓冲池（TTL/补水/降级/健康告警/软重启）、步骤级重试、账号去特征化、Provider 适配层、双轨容灾回退、自检容器隔离与配置校验。
- **CLI**：新增 `--sidecar-check`（Sidecar 可用性诊断）与 `--sidecar-produce`（真实产出一次 Turnstile + Castle Token，并含官方测试 key 自检；只打印长度，不打印 Token 内容）；`--check` 输出增加 `sidecar` 字段；Sidecar 模式下不再强制要求 `CAPSOLVER_API_KEY`。
- **依赖**：`pyproject.toml` 增加 `sidecar`（playwright）与 `faker` 两个可选 extra。

### 6.2 修掉一个早已失效的 `uv.lock`

仓库里的 `uv.lock` 是**孤儿文件**，与 `pyproject.toml` 完全对不上，且在我动手之前就已经失效：

| | 项目名 | 版本 | 包数 |
|---|---|---|---|
| `pyproject.toml` | `grok-reg-protocol` | 1.0.0 | 1 个直接依赖（`curl_cffi`） |
| 原 `uv.lock` | `grok-reg-protocol-cpa` | 1.1.0 | 29 个（含 `drissionpage`/`lxml`/`openpyxl`/`tldextract` 等 DrissionPage 时代残留） |

在上游 base 提交上执行 `uv lock --check` 即可复现失败（`The lockfile at uv.lock needs to be updated`），也就是说它与本次改动无关，是历史遗留。

危害是实打实的：任何 `uv run` / `uv sync` 都会按这份错误锁文件同步环境——本次定位 flaky 断言时就现场撞上了，`uv run` 把已装好的 `curl-cffi` 从 0.16.3 **静默降级**到 0.15.0 并移除了 `faker`。

已重新生成：15 个包，`uv lock --check` 通过，并用 `uv run --frozen` 按锁定版本复跑 75 用例全绿。顺带确认 `curl-cffi` 0.15.0 的 impersonate 目标已覆盖 `chrome146`，指纹版本窗口（131–150）在两个版本下均可用。

- **仓库卫生**：新增 `.gitattributes`（统一 LF，避免 Windows 整文件级 diff）；`.gitignore` 补充 `.env.*` 与 `.workbuddy-ai/`。

### 6.1 修掉一个 flaky 断言（交付前复跑暴露）

`ProfileDeFingerprintTest::test_password_has_no_fixed_affixes` 原写法是「40 个样本中不得出现 `N!` 开头 / `#7` 结尾」。

问题在于修复后前后缀已是均匀随机，偶发命中属正常概率事件：20 万次采样实测命中率各约 **0.02%**（`1/76²`，即 1/5776 量级），换算到 40 个样本，原断言**单次运行约 1% 概率误报失败**。这不是产品缺陷，是断言口径错了——它要求随机生成器"永不"产出某个值，而随机生成器给不了这个保证。

已改为断言"不是恒定模式"，两条证据互补：

- **决定性**：400 个样本的首两位须分散出 >100 种取值（原实现恒定只有 `N!` 一种）；
- **统计性**：`N!` / `#7` 命中数须远低于样本数的 10%。

修复后连跑 5 次，75 用例全绿。

---

## 七、验证方式与实测结果

```bash
# 1. 单元测试（连跑 5 次确认无 flaky）
python -m unittest discover tests          # 75 passed

# 2. 无头浏览器与 Harness 自检
python main.py --sidecar-check

# 3. 真实产出一次 Turnstile + Castle Token（含官方测试 key 自检）
python main.py --sidecar-check --sidecar-produce
```

本机（Windows + 本机 Chrome，直连无代理）实测：

```json
{
  "harness": {
    "turnstile": true, "castle": true, "castleConfigured": true,
    "webdriver": null, "hardwareConcurrency": 8,
    "viewport": {"width": 1280, "height": 800}, "languages": ["en-US", "en"]
  },
  "self_test": { "ok": true, "token_length": 21 },
  "turnstile_error": "Turnstile 未创建挑战 iframe（widget 静默失败）…",
  "castle_error": "Castle createRequestToken timeout"
}
```

结论：

1. **Sidecar 本身验证通过**：用官方 always-pass 测试 key 走完整链路（Harness 路由劫持 → 无头 Chromium → Turnstile SDK → token 回传）真实拿到了 token，stderr 无 greenlet 报错。
2. **真实 sitekey 在本机直连环境下不渲染**：真实 key 会做风险评估（测试 key 不会），Cloudflare 对当前出口 IP/环境直接不下发挑战。这与设计文档「Turnstile / Castle / 注册 RPC 必须同一条住宅代理」的前提一致，**配置 `PROXY` 后复测即可**；失败时会触发 `proxy_unhealthy` 告警。
3. **Castle 同样受环境影响**：`createRequestToken` 60s 内不兑现且无网络请求，现在会给出明确超时错误并自动回退到 Node SDK / 远程供应商。

---

## 八、兼容性与破坏性变更

- **无破坏性变更**。`USE_LOCAL_SIDECAR` 默认 `false`，原生链路行为与 v1.0.0 一致（仅修掉了 CapSolver 的代理透传 bug）。
- 行为差异提醒：默认 `Accept-Language` 由 `zh-CN` 变为 `en-US`；`sec-ch-ua` 与 UA 主版本改为随机轮换。若下游有依赖固定指纹的逻辑需注意。

## 九、后续可选项

- 用正式 `auth_mgmt.proto` + `protoc`/`betterproto` 生成编解码代码，替换手写 varint（本次仅做了 wire type 扩展，未引入代码生成依赖）；
- `BaseMailProvider` / `BaseCaptchaProvider` 抽象与多供应商热插拔（22.do、TempMail、YesCaptcha、2Captcha…）；
- 图形选择类交互挑战的自动识别与代理自动轮换。
