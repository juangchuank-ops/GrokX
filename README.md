# GrokX

基于 HTTP + gRPC-Web 纯协议实现的 x.ai / Grok 账号注册工具。

[![GitHub stars](https://img.shields.io/github/stars/huey1in/GrokX)](https://github.com/huey1in/GrokX/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/huey1in/GrokX)](https://github.com/huey1in/GrokX/network)
[![release](https://img.shields.io/badge/version-1.1.0-blue)]()
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![curl_cffi](https://img.shields.io/badge/curl_cffi-%3E%3D0.7-4B8BBE)]()
[![Node](https://img.shields.io/badge/Node.js-22.13%2B-339933?logo=node.js&logoColor=white)]()
<a href="https://linux.do"><img src="https://img.shields.io/badge/LINUX%20DO-社区-f0b752?style=flat-square" alt="LINUX
   DO"></a>

## 简介

`GrokX` 是一个纯协议实现的 x.ai（Grok）账号注册工具：通过 `curl_cffi` 模拟 Chrome TLS 指纹，直接向 x.ai 的 gRPC-Web 后端接口发送注册 RPC，无需启动浏览器。整合 MoeMail 临时邮箱、人机验证、Castle 反滥用令牌等服务，自动完成从创建邮箱到导出 SSO 会话凭据的整条链路，支持多线程批量注册。

人机验证与反滥用令牌有**两套可切换的实现**：

| 模式 | 人机验证 | Castle 令牌 | 成本 / 一致性 |
|------|----------|-------------|----------------|
| 原生（默认） | CapSolver（`AntiTurnstileTask`，透传代理） | Node.js + JSDOM 运行官方 SDK | 按次计费；JSDOM 环境熵较低 |
| **本地 Sidecar**（`USE_LOCAL_SIDECAR=true`） | 本地无头 Chromium 真实求解 | 同一浏览器上下文真实提取 | **零打码成本**；Turnstile/Castle/注册 RPC **同一条代理 IP 闭环** |

## 工作原理

浏览器里完成的注册，在 `GrokX` 中被拆解为一组**协议级调用**：

```
你的机器 ── curl_cffi(Chrome 指纹) ──▶ https://accounts.x.ai
   │                                    │  /auth_mgmt.AuthManagement (gRPC-Web)
   ├─ MoeMail  ── 创建临时邮箱 / 收验证码 ─┘
   ├─ 人机验证 ── Turnstile Token  ────────┘   (CapSolver 或本地 Sidecar)
   └─ Castle ──── 反滥用 Request Token ────┘   (Node JSDOM 或本地 Sidecar)
```

核心思路：

- **协议客户端**（`registration/protocol_client.py`）手写实现 protobuf 字段编码与 gRPC-Web 帧解码，向 `CreateEmailValidationCode` / `VerifyEmailValidationCode` / `CreateUserAndSessionV2` 等 RPC 发送请求。
- **TLS 指纹**（`network/fingerprint.py`）从 `curl_cffi` 实际支持的 impersonate 目标里挑版本，再由该版本反推 User-Agent 主版本（保证 **TLS 与 UA 版本一致**），`sec-ch-ua` 的 GREASE 品牌与顺序随机轮换，`Accept-Language` 可随代理出口区域配置。
- **反滥用链路**：发信与注册两个阶段各需要一个 Castle Request Token；原生模式通过官方 `@castleio/castle-js` SDK 在 Node（jsdom）中实时生成，Sidecar 模式则从真实 Chromium 上下文提取。
- **步骤级重试**：每个阶段都带指数退避重试，收信延迟或 RPC 偶发 429/502 不再让整条链路作废。

## 注册流程

一条完整的注册共 **11 个阶段**：

```text
1. 初始化注册任务
2. 建立协议会话           bootstrap 注册页
3. 创建临时邮箱           MoeMail 生成一次性收件箱
4. 生成邮件阶段 Castle Token
5. 发送邮箱验证码         CreateEmailValidationCode RPC
6. 获取邮箱验证码         轮询 MoeMail 收件箱并提取
7. 确认邮箱验证码         VerifyEmailValidationCode RPC
8. 完成人机验证           Turnstile（CapSolver 或本地 Sidecar）
9. 生成注册阶段 Castle Token
10. 提交账号注册请求       CreateUserAndSessionV2 RPC
11. 获取 SSO 凭据          从响应 Cookie / 消息中提取 sso
```

## 项目结构

```text
GrokX/
├── main.py                        # CLI 根入口
├── registration/
│   ├── cli.py                     # 命令行：并发、进度、策略路由、结果落盘
│   ├── flow.py                    # 注册状态机（11 阶段编排 + 步骤级重试）
│   └── protocol_client.py         # gRPC-Web 协议客户端（protobuf 编解码 + RPC）
├── providers/
│   ├── castle.py                  # Castle 令牌提供者 + MoeMail 适配
│   ├── castle_sdk/                # Node 子包：用官方 Castle JS SDK 生成 token
│   │   ├── mint.mjs
│   │   └── package.json
│   ├── capsolver.py               # CapSolver 求解 Turnstile（支持代理透传）
│   ├── local_sidecar.py           # 本地 Sidecar 的 Provider 适配层
│   ├── mail.py                    # MoeMail OpenAPI 客户端
│   └── turnstile_flow.py          # 挑战上下文 / 已获取令牌模型
├── sidecar/                       # 本地无头浏览器 Sidecar（可选）
│   ├── harness.html               # accounts.x.ai 域下的轻量宿主页
│   ├── browser_worker.py          # Playwright 常驻实例（专用线程 + 命令队列）
│   ├── token_pool.py              # Turnstile 缓冲池（TTL 淘汰 + 自适应补水）
│   └── service.py                 # 单例门面 + 降级信号
├── network/
│   ├── fingerprint.py             # Chrome 指纹生成（TLS/UA 版本对齐）
│   └── proxy.py                   # 代理 URL 解析 / 归一化 / 脱敏
├── config/
│   └── loader.py                  # .env 加载
├── tests/                         # 单元测试
└── output/                        # 注册结果（gitignored）
```

## 环境要求

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.11 | 运行环境 |
| [curl_cffi](https://pypi.org/project/curl_cffi/) | ≥ 0.7 | Chrome TLS 指纹 HTTP 客户端 |
| Node.js | ≥ 22.13 | 运行 Castle SDK（原生模式生成反滥用 token） |
| [playwright](https://pypi.org/project/playwright/) | ≥ 1.40 | **仅 Sidecar 模式需要**；复用系统已装的 Chrome/Edge，无需下载内核 |
| [uv](https://docs.astral.sh/uv/)（可选） | — | 依赖管理 |

## 安装

```bash
# 1. Python 依赖
uv sync              # 或：pip install "curl_cffi>=0.7"

# 2. Castle SDK 的 Node 依赖（仅原生模式需要）
cd providers/castle_sdk && npm ci && cd ../..

# 3. Sidecar 模式的浏览器依赖（可选）
uv sync --extra sidecar     # 或：pip install "playwright>=1.40"
# 若本机没有 Chrome/Edge，再执行：playwright install chromium
```

## 配置

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
```

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `MOEMAIL_API_BASE` / `MOEMAIL_API_KEY` | ✅ | MoeMail 临时邮箱服务 |
| `CAPSOLVER_API_KEY` | ⚠️ | CapSolver 人机验证（原生模式必填；Sidecar 模式可不填） |
| `USE_LOCAL_SIDECAR` | ❌ | `true` 启用本地无头浏览器 Sidecar，替代 CapSolver + Node JSDOM |
| `PROXY` / `PROXY_ENABLED` | ❌ | 代理（`socks5://user:pass@host:port` 等；留空则直连） |
| `PROTOCOL_PAGE_URL` | ❌ | 注册页地址（默认 `https://accounts.x.ai/sign-up`） |
| `PROTOCOL_TURNSTILE_ACTION` / `PROTOCOL_TOS_ACCEPTED_VERSION` | ❌ | Turnstile action / ToS 版本 |
| `PROTOCOL_TURNSTILE_SITEKEY` / `PROTOCOL_CASTLE_PUBLISHABLE_KEY` | ❌ | 前端公钥覆盖项（留空用内置默认值，便于官方轮换时免改代码） |
| `SIDECAR_HEADLESS` / `SIDECAR_POOL_SIZE` / `SIDECAR_MAX_AGE_SEC` | ❌ | Sidecar 运行参数（池容量默认 2，Token TTL 默认 240s） |
| `SIDECAR_BROWSER_CHANNEL` / `SIDECAR_LOCALE` / `SIDECAR_TIMEZONE` | ❌ | 指定浏览器渠道（`chrome`/`msedge`）与语言/时区 |
| `SIDECAR_TURNSTILE_TIMEOUT` | ❌ | 单次 Turnstile 求解超时（默认 30s） |
| `SIDECAR_CASTLE_TIMEOUT` | ❌ | 单次 Castle Token 提取超时（默认 20s） |
| `STEP_ATTEMPTS` / `STEP_BACKOFF` / `MAX_STEP_BACKOFF` | ❌ | 状态机步骤级重试次数与退避上限 |
| `FINGERPRINT_MODE` / `ACCEPT_LANGUAGE` / `FINGERPRINT_REGION` | ❌ | 指纹模式与语言偏好（应与代理出口区域一致） |
| `MOEMAIL_DOMAIN` / `MOEMAIL_EXPIRY_TIME` / `MOEMAIL_USE_PROXY` | ❌ | MoeMail 细节 |
| `CAPSOLVER_TIMEOUT_SEC` / `CAPSOLVER_POLL_INTERVAL_SEC` | ❌ | 人机验证超时/轮询 |
| `CASTLE_PROVIDER_URL` / `CASTLE_PROVIDER_KEY` | ❌ | 可选：改用远程 Castle token 供应服务 |
| `CASTLE_EMAIL_TOKEN` / `CASTLE_FINAL_TOKEN` | ❌ | 可选：直接使用静态 token（跳过 SDK） |
| `DEFAULT_DOMAINS` | ❌ | 默认邮箱域名（`config.loader` 使用） |

## 使用

```bash
# 注册 1 个账号
python main.py

# 批量注册 10 个，4 线程并发
python main.py -n 10 -j 4

# 只检查配置是否齐全，不发请求
python main.py --check

# 测试代理连通性
python main.py --proxy-check

# 诊断本地 Sidecar（启动无头浏览器 + 加载 Harness + 环境自检）
python main.py --sidecar-check

# 连同真实产 Token 一起验证（只输出 Token 长度，不打印 Token 内容）
python main.py --sidecar-check --sidecar-produce

# JSONL 事件输出（便于外部集成/日志收集）
python main.py --events

# 自定义结果文件路径
python main.py --output-json path/to/result.json
```

命令行参数：

| 参数 | 说明 |
|------|------|
| `--env <path>` | `.env` 路径（默认项目根目录 `.env`） |
| `-n, --count <n>` | 注册数量（默认 1） |
| `-j, --jobs <n>` | 并发任务数（默认 1，受 `-n` 约束） |
| `--check` | 检查必需配置，退出 0/2 |
| `--proxy-check` | 代理连通性测试 |
| `--sidecar-check` | 本地 Sidecar 可用性诊断 |
| `--sidecar-produce` | 配合 `--sidecar-check` 真实产出一次 Turnstile + Castle Token |
| `--events` | 输出 JSONL 进度事件（不输出人类可读日志） |
| `--output-json <path>` | 结果文件路径（默认 `output/web_register_result.json`） |

### 输出

每个成功注册的账号写入：

- **`output/web_register_result.json`** — 追加式数组，每条含：

  ```json
  {
    "created_at": "2026-08-16T14:57:34.575846+00:00",
    "email": "sj8x2x71ukht@91dick.com",
    "password": "N!UnGjzTv5wSnlezOQGl#7",
    "sso": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
  }
  ```

- **`output/web_register_result.txt`** — 纯 SSO token，每行一个，便于直接导入。

## 本地无头浏览器 Sidecar

启用 `USE_LOCAL_SIDECAR=true` 后，Turnstile 与 Castle 令牌都改由**本机真实 Chromium** 产出，不再依赖 CapSolver 与 JSDOM。

### 它解决了什么

| 问题 | 原生模式 | Sidecar 模式 |
|------|----------|--------------|
| 打码成本 | 每次约 $0.0015~0.002 | **0（全本地）** |
| Token 获取延迟 | 5~15s 轮询打码 API | **0ms 出队**（缓冲池预热） |
| Turnstile 的 IP | 打码平台机房 IP，与注册 IP 脱节 | **与注册 RPC 同一条代理** |
| Castle 设备熵 | JSDOM 无 Canvas/WebGL/音频指纹 | **真实渲染管线**（真实 GPU/Canvas 熵） |
| 并发开销 | 每次注册冷启动 Node 进程 | 常驻实例 + 后台补水 |

### 关键实现

1. **域名白名单绕过（Origin Spoofing）**：Turnstile 会校验 `window.location.origin`。Sidecar 用 Playwright 路由拦截，把 `https://accounts.x.ai/__turnstile_harness__` 直接 fulfill 成本地 `harness.html`，浏览器地址栏与 DOM 上下文归属真实注册域，且不产生真实网络请求。
2. **专用浏览器线程**：Playwright 同步 API 的事件循环绑定在创建它的线程上，跨线程调用会抛 `greenlet.error: Cannot switch to a different thread`。因此 `BrowserWorker` 把所有浏览器操作投递到一条专用线程的命令队列里串行执行，对外仍是同步方法，可被补水线程与并发业务线程安全调用。
3. **Token 缓冲池**：FIFO 队列 + 240s TTL 淘汰 + 自适应补水；连续 3 次失败上报 `proxy_unhealthy` 并软重启 Context，累计产出 100 个 Token 后自动重建 Context 清理内存；失败越多退避越久，避免在坏环境下占满浏览器线程。
4. **代理闭环**：浏览器实例挂载与 `PROXY` 相同的出口（`socks5h` 自动转为 Chromium 可用的 `socks5`），使「Turnstile 求解 / Castle 上报 / 发信 RPC / 注册 RPC」四者出口一致。
5. **混合双轨容灾**：Sidecar 启动失败（缺依赖、无 Chromium）时回退到 CapSolver + Node SDK；即使 Sidecar 已启动，单个环节失败也会自动回退到备用 Provider（`provider_fallback` 事件），流水线不中断。
6. **两个官方 SDK 的坑（实机验证才暴露）**：
   - Turnstile 的 `api.js` **不能**带 `async defer`，否则 `render()` 静默失败——不建 iframe、不触发 error-callback、token 永远不返回；
   - Castle 的 `createRequestToken()` 返回的是**只有 `then`、没有 `catch`** 的自定义 thenable，必须用 `then(onFulfilled, onRejected)` 两参形式，并加 JS 侧超时。

### 实机验证

```bash
python main.py --sidecar-check --sidecar-produce
```

本机（Windows + 本机 Chrome）实测结果：

```json
{
  "harness": {
    "turnstile": true, "castle": true, "castleConfigured": true,
    "webdriver": null, "viewport": {"width": 1280, "height": 800},
    "languages": ["en-US", "en"]
  },
  "self_test": { "ok": true, "token_length": 21 },
  "turnstile_error": "Turnstile 未创建挑战 iframe（widget 静默失败）…",
  "castle_error": "Castle createRequestToken timeout"
}
```

- **`self_test.ok = true`**：用 Cloudflare 官方 always-pass 测试 key（`1x00000000000000000000AA`）走完整链路，**真实拿到了 Turnstile token**，证明 Harness、路由劫持、无头环境、轮询与回传全部正常。
- **`turnstile_error`**：真实 sitekey 下 widget 不渲染。真实 key 会做风险评估，而测试 key 不会——在本机直连（无代理）环境下 Cloudflare 直接不下发挑战。这正是文档强调「Turnstile / Castle / 注册 RPC 必须走同一条住宅代理」的原因，**配置 `PROXY` 后复测即可**。
- **`castle_error`**：Castle 的 `createRequestToken` 在本机环境下 60s 内不兑现且无任何网络请求，同样指向出口 IP/环境被风控；此时会自动回退到 Node SDK 或远程供应商。

> 也就是说：**Sidecar 本身已验证可用**，`--sidecar-produce` 的 `self_test` 字段专门用于把「Sidecar 故障」与「出口 IP 被风控拒绝」区分开——如果 `self_test.ok` 为 true 而真实 key 失败，问题在代理出口，不在代码。

## 测试

```bash
python -m unittest discover tests
```

当前覆盖 **75 个用例**，包括：protobuf/gRPC-Web 字节级编解码、`CreateUserAndSessionV2` 嵌套字段布局回放、指纹 TLS/UA 一致性、CapSolver 代理透传、Token 缓冲池 TTL/补水/降级、状态机步骤级重试、账号去特征化生成、Sidecar 双轨容灾回退与自检容器隔离。

## 合规提示

本项目仅用于协议研究与自动化技术学习。批量注册账号通常违反目标平台的服务条款，请自行评估合规风险，不要用于垃圾信息、欺诈或其他侵害他人权益的场景。

## 贡献指南

欢迎提交 PR 与 Issue。请遵循以下约定：

1. **分支**：从 `main` 拉取独立功能分支（`feature/xxx` 或 `fix/xxx`），不要直接往 `main` 提交。
2. **风格**：与现有代码保持一致——中文注释、模块级 `from __future__ import annotations`、函数自带类型标注。
3. **协议改动**：涉及 `registration/protocol_client.py` 的字段编码或 RPC 方法时，务必对照抓包的 protobuf 字段号，改动需可回放验证。
4. **测试**：提交前确保 `python -m unittest discover tests` 全部通过；新增逻辑尽量补充单元测试。
5. **提交信息**：简洁描述改动，遵循 Conventional Commits（`feat:` / `fix:` / `docs:` / `refactor:`）。
6. **PR 说明**：说明改动动机、验证方式；不要提交 `.env`、`output/` 等敏感或生成文件。
