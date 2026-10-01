# minimaxcode2api

> MiniMax Agent 多账号反代网关 — 账号池调度、设备指纹对齐官方客户端、双认证路径（Web JWT / 设备流 OAuth）、浏览器一键登录、支持 OpenAI / Anthropic 协议与标准客户端。

## 📝 更新日志

### 本次更新（改用官方直连模型 API，彻底告别黑盒 Agent 的 echo / 卡顿 / 上下文爆炸）

- **重大变更：上游从「Agent 会话接口」切换为「直连模型 API」**。此前请求的是 MiniMax Code 的 Agent 会话端点（`/minimax-cloud/api/v1/session/{id}/message`，单段 `content` 字符串），那是一条黑盒链路：会把整轮 prompt 原样回显、把回答重复两遍、每轮全量重发上下文，切换模型后单请求可达 38 万 token、首字节迟滞数十秒。现改为官方客户端真正使用的直连模型端点：
  ```
  POST https://agent.minimax.io/mavis/api/v1/llm/v1/messages
  ```
  这是标准 Anthropic Messages 协议（结构化 messages、tool use、SSE），**无会话、无回显、无重复、上下文由客户端自管**。
- **双协议对外**：`/v1/messages`（Anthropic）原样透传；`/v1/chat/completions`（OpenAI）在 `app/model_api.py` 中做双向转换（含流式：Anthropic SSE → OpenAI SSE，覆盖 text / thinking / tool_calls）。
- **鉴权**：仅需 `Authorization: Bearer <token>`（与既有账号池 OAuth 完全一致），无需 `x-signature` / `yy` 签名。
- **移除**：黑盒 Agent 的 `build_prompt` 拍平、`StreamingEchoFilter` 回显剥离、`_is_repeat_tail` 重复抑制、伪流式队列桥接等一整条链路（不再需要）。
- **账号探测**改为一次最小的 `Messages` 调用，直接验证模型的 Bearer token。
- 模型目录沿用上游真实 id：`MiniMax-M3.1-Flash-Preview` / `MiniMax-M3` / `MiniMax-M2.7(-highspeed)` 等。

### 本次更新（真流式转发 · 修复“一直 wait response”）

- **修复：流式请求长时间无任何输出，客户端超时后 502**。`gateway._stream` 此前是**伪流式**——先 `await self._run(...)` 等上游把整轮生成完，再一次性吐出所有帧。上游在轮次之间会保持连接，于是首字节延迟等于整个生成耗时；只发一句 `hi` 也要等上游完整返回，客户端（opencode / Grok CLI）等不到首字节便断开，审计记为 `502 Cancelled via cancel scope`、`latency_ms=0`。改用 `asyncio.Queue` 桥接：上游调用作为任务运行，逐 delta 入队，生成器边到边转发，**首字节延迟从数十秒降到毫秒级**。
- **流式回显过滤**：真流式下无法等全文再剥回显，新增 `upstream.StreamingEchoFilter`——先缓存开头若干 delta，判定“回复是否以上游回显的 prompt 开头”，确认是回显则丢弃，否则整体放行；客户端不会短暂看到请求内容。流结束时 `flush` 兜底。
- **流式重复回答抑制**：上游在轮末会把整段回答作为**单个 chunk 重发一次**，导致内容与 token 翻倍。`gateway._is_repeat_tail` 检测“该 chunk 是否以已发内容开头”，命中则丢弃。
- 流式路径现在同样写入审计（`response_body`、`completion_tokens`）与用量统计。

### 本次更新（剥离上游回显·切换模型不再撑爆上下文）

- **修复：切换模型后响应里出现整段 system prompt / skills，且 token 虚高**。部分 MiniMax Agent 上游会把本轮请求的 `content`（网关拍平的 system prompt + 历史）**原样回显**成一帧，`_handle_frame` 用 `CONTENT_KEYS`（含 `content`）把它当成回答收集。表现是只发一句 `hi`，回复里却带着几十万 token 的 `[系统指令] You are opencode... / <available_skills>...`，`completion_tokens` 虚高到 ~38 万，耗时数十秒。
- **动态剥离回显（非硬截断）**：新增 `upstream._strip_prompt_echo`，逐帧检测——只有当某帧**确实包含本次请求的 prompt**（≥32 字符且完整匹配）时才剥掉这段回显，前后真实文本都保留；短 prompt（如 `hi`）不具备判别性，不处理，正常回答不受影响。流式时逐帧剥，`gateway._finalize` 组装完成后再用完整 prompt 兜底剥一次（应对跨帧回显）。
- **折叠重复回答**：`upstream._collapse_repeated_tail` 把上游偶尔整段重复两遍的回答（`Hi!...Hi!...`）折叠为一份；仅对**整段完全翻倍**且 ≥16 字符生效，真实回答内的正常重复不受影响。
- 新增 3 个回归测试覆盖以上行为。

### 本次更新（模型选择 · 上新 M3.1 Flash）

- **接入真实模型选择**：`ModelConfig.upstream_model` / `variant` 现在会转换为上游的 `model` 选择对象 `{"model_id", "provider_id": "minimax", "variant"}`（此前从未发送，所有请求都用账号默认模型）。上游拒绝裸字符串，必须是对象。
- **新增模型**：`minimax-m3.1-flash`、`minimax-m3.1-flash-thinking`（上游 `MiniMax-M3.1-Flash-Preview`，512K / 可选 1M）；修正 `minimax-m3-thinking` 使用 `MiniMax-M3` + `variant=thinking`。
- **实时读取上游模型**：`GET /minimax-cloud/api/v1/config` 返回当前可用模型清单。

### 本次更新（注册 · 密码 · 邮箱限流）

- **无头注册支持邮箱密码**：默认给新账号设密码 `Minimax2026!`（`signup.password`），注册面板新增“初始密码”输入框，填了则覆盖默认值，留空则用默认；默认留空且面板留空时不设密码。
- **未配代理池不再报错**：代理池为空时注册回退到本机出口（`upstream.proxy`，未填则直连），只有“池内代理全部超地址预算”才拒绝，修复了此前误报 `no usable proxy with address budget left`。
- **邮箱 admin passkey 不再被误解**：邮箱配置面板不再把掩码 `********` 回填成“已填”的假值（改为留空 + “已设置，留空则不变”）；worker 拒绝密钥时抛出明确中文提示（`AUTH_ADMIN_CREDENTIAL_INVALID`）；缺失/占位符密码导致注册失败的问题一并修复。
- **注册“卡在 setting password”说明**：设密码需第二封验证码，发码被限流（code 32）时程序会等待重试。README 新增“排错：注册卡在 setting password / code 32”一节，给出换出口、放慢批量、跳过密码等解决方案。

### 本次更新（账号池健康 · 保活 · 额度 · 签到）

- **取消请求泄漏修复**：流式请求被客户端中断时，`CancelledError` 会绕过 `pool.release`，导致账号的 `inflight` 永久 +1。单账号 `max_concurrent=1` 时整个号池被钉死，之后所有 chat 卡住且无日志。现在 `gateway._run` 用 `try/finally` 保证租约必定归还。
- **Token 保活（keepalive）**：
  - Access token 实测存活 **1 小时**（`expires_in=3600`），refresh token 每次续期轮换。
  - 保活巡检 **30 分钟一次**（`keepalive.interval_sec`），提前 **40 分钟**续期（`REFRESH_MARGIN_SEC`），失败 120 秒后重试——不高频打扰 RT 端点。
  - 续期优先用 refresh token；失败/缺失时**自动回退到邮箱+密码登录**，并重新铸造 refresh token。
  - 只保活 **enabled** 账号，停用账号不占用请求。
- **额度（credits）获取与保活分离**：额度**每 5 分钟**独立刷新（`signin.credit_refresh_min`），含停用账号；余额改为 **float**，不再截断小数（如 `1492.125`）。
- **签到**：
  - 修复 `claimed_today` 判定写反（`status==1` → `status==3`）——此前"已签"被当成"未签"，反复请求 claim。
  - 签到按钮**可反复点击**，已签返回 `already`（幂等，不重复发放）；签到后**重读余额**并在控制台显示最新值。
  - **注册成功后自动签到一次**，领取首日额度；之后由控制台手动签到。
- **导入来源区分**：账号新增 `source` 字段（`signup` / `password` / `device` / `token`），控制台以标签显示，明确哪些可凭邮箱密码重登、哪些依赖 refresh token。
- **邮箱密码批量导入**：控制台新增输入框，每行 `邮箱 密码` 或 `邮箱----密码`，导入时自动登录换取 token + refresh token 入池。
- **网页登录导入修复**：设备流响应中的 refresh token 之前被丢弃，导致网页导入的账号"不可续"。现在会连同 `expires_in` 一起保存。
- **控制台合并与提示**：首页即号池（概览面板并入）；余额、签到、状态列显示相对时间与红色警示（掉凭据的 enabled 账号标红）。
- **安全脱敏**：邮箱服务地址/域名/密钥从代码与示例中移除，改由环境变量提供。

## 🔧 官方客户端指纹模拟

代理请求默认模拟官方客户端的请求特征，避免流量特征与官方差异过大触发风控：

- **签名算法与官方逐字对齐**：`x-signature = md5(unix + salt + body)`、`yy = md5(encodeURIComponent(path) + "_" + body + md5(ms) + "ooui")`，盐值取自官方 bundle
- **两套身份按凭据类型分派**（不是全局开关）：
  - Web JWT 账号 → `client=web`、`os_name=Windows`、`browser_platform=Win32`、浏览器 UA
  - 设备流 OAuth 账号 → `client=desktop`、`is_desktop=1`、`os_name=macOS`、`browser_platform=MacIntel`、`MiniMaxAgent/3.0.73` UA、**不发 `accept-language`**
- **22 个查询参数按官方顺序手写**：`yy` 是对 URL 的摘要，排序会改变签名，故不做任何键排序处理
- **`unix` 与签名共用同一次时钟读数**：URL 的 `unix` 与 `yy` 的 `ms` 必须一致，否则签名描述的是一个未发出的 URL
- **签名目标按请求类型区分**：普通调用签相对路径，流式调用签绝对 URL（官方 builder 仅在 stream 时把 URL 升级为绝对地址并切换 `agent-stream` 主机）
- **`realUserID` 不在 JWT 中**，由 `/v1/api/user/info` 补全；该端点是唯一可无 `user_id` 调用的引导接口

## 项目简介

一个本地 API 代理服务，将 **MiniMax Agent 网页版**（`agent.minimax.io`）的底层接口转换为标准的 OpenAI / Anthropic 协议格式。

**核心能力：**

- **多账号池**：每个账号是一份登录凭据（JWT 或 OAuth access token），按策略调度、故障转移、健康冷却
- **浏览器一键登录**：OAuth2 设备码流程（PKCE S256），点按钮跳转官方授权页，登录完成后账号自动入池，无需手动复制 `_token`
- **协议转换**：OpenAI Chat Completions + Anthropic Messages，支持流式（SSE）
- **媒体本地化**：上游生成的图片 URL 几小时后过期，自动下载到本地并按 `/media/<id>` 提供
- **每日签到**：多账号自动签到、积分核查、大陆区账号自动跳过；注册成功自动签一次，其余可在控制台手动签到
- **Token 保活**：refresh token 优先、邮箱密码兜底，30 分钟巡检、提前 40 分钟续期，账号不会因一小时 token 过期而死
- **额度刷新**：每 5 分钟独立刷新余额（含停用账号），控制台显示最近读取时间
- **管理控制台**：单文件 HTML（无构建步骤），号池 / 模型 / 签到 / 审计 / 画廊 / 设置

## 目录结构

```
minimaxcode2api/
├── app/
│   ├── config.py          # 设置（六分组，SQLite 持久化，运行时热更新）
│   ├── signing.py         # x-signature / yy / encodeURIComponent 对齐实现
│   ├── upstream.py        # MiniMax 上游客户端、双身份指纹、协议常量
│   ├── gateway.py         # OpenAI / Anthropic 协议转换与故障转移
│   ├── pool.py            # 账号池调度、冷却与健康状态机
│   ├── device_login.py    # OAuth2 设备码登录流程
│   ├── signup.py          # 无头注册（邮箱验证码 + 自动设备码授权）
│   ├── proxy.py           # 代理池：解析 / 检测 / 轮换
│   ├── env.py             # 环境变量覆盖与 .env 读取
│   ├── signin.py          # 每日签到与积分核查
│   ├── keepalive.py       # OAuth token 保活（refresh / 密码兜底）
│   ├── admin.py           # 管理 API
│   ├── media.py           # 生成媒体下载与本地索引
│   ├── prompt.py          # OpenAI 消息 → 上游单串 prompt
│   ├── db.py              # SQLite 持久化（账号 / 审计 / 设置 / 媒体）
│   ├── records.py         # 数据模型
│   ├── server.py          # FastAPI 路由
│   ├── security.py        # 标识符与指纹生成
│   ├── tokens.py          # JWT 解析与区域推断
│   └── static/index.html  # 单文件控制台
├── tests/
├── run.py
├── .env.example
└── requirements.txt
```

## 安装与启动

```bash
pip install -r requirements.txt
python run.py --port 4555
# 控制台: http://127.0.0.1:4555/admin
```

默认端口 `4555`，默认监听 `127.0.0.1`。因为没有任何鉴权，**不要改成对外地址**；需要外部访问请置于带鉴权的反代之后（`run.py` 在监听非本机地址时会打印告警）。

## 快速开始

### 1. 导入账号

两种方式，任选其一：

**方式 A：浏览器一键登录（推荐）**

控制台「号池」→ 选择区域 →「开始登录」→ 跳转 MiniMax 官方授权页 → 登录并授权 → 账号自动入池。

| | 海外版 | 国内版 |
|---|---|---|
| **Agent 站点** | `agent.minimax.io` | `agent.minimax.cn` |
| **账号服务** | `account.minimax.io` | `account.minimax.cn` |
| **授权页** | `account.minimax.io/oauth-authorize` | `account.minimax.cn/oauth-authorize` |
| **适用账号** | 邮箱 / 海外手机号 | 国内手机号 |

- 走官方 OAuth2 设备码流程（RFC 8628 + PKCE S256），**不监听端口、不读取 Cookie**
- 两个区域是独立的账号体系与独立的账号服务，凭据互不通用；区域在登录时选择，与账号一同存储
- 凭据类型为 `oauth`，认证走 `Authorization: Bearer`，指纹自动切换为桌面客户端
- Access token 有效期有限（响应中的 `expires_in`），过期需重新授权

**方式 B：手动粘贴 JWT**

浏览器登录 `https://agent.minimax.io` → DevTools → Application → Local Storage → 复制 `_token` → 粘贴到控制台导入框（每行一个）。

- 凭据类型为 `token`，认证走 `token=` 查询参数，指纹为浏览器形态
- 也可写成 JSON 补充指纹：`{"token":"eyJ...","uuid":"...","device_id":"12345678"}`

导入时若勾选「自动探测」，会调用上游读取 `realUserID` 与 agent 列表，解析出 `agent_id`（优先 `mavis` 角色）。逐账号约 1–3 秒。

> **网络要求**：海外版业务接口锁海外出口，本机不在海外时请先在「设置」中填写 `proxy`，否则探测必定 401；国内版直连即可。该设置只影响代理，账号服务本身可直连。回环地址自动绕过代理。

**方式 C：无头注册（自动造号入池）**

控制台「号池」→「无头注册」→ 选区域 / 数量 /（可选）初始密码 →「开始注册」。全程无浏览器：

1. 向临时邮箱服务要一个地址（`signup.mail_base` / `mail_domain` / `mail_pass`）；
2. `POST /v1/api/user/login/sms/send` 取邮箱验证码；
3. `POST /oauth2/login`（`loginType=21`）验证即注册，拿到 `_sid` 会话；
4. **设置密码**：默认使用 `signup.password`（默认 `Minimax2026!`），注册面板填了则以面板为准。这一步需要**第二封邮件验证码**（注册那封已被消耗），通常比前面慢；
5. 服务端用该会话走**设备码流程**（`/oauth2/device/code` → `GET/POST /oauth2/device/authorize` → `/oauth2/token`）换出 OAuth `access_token` + `refresh_token`；
6. 交给现有 `import_device_token` 入池，并**自动签到一次**领取首日额度。

> 该 build 的腾讯验证码被编译关闭（`h.Xy=false`），故发码无需验证码；`cn` 区 build 会拉起验证码，是另一条路。

> **密码步骤可跳过**：把 `signup.password` 设为空、且注册面板留空，则不设密码；账号仍有 refresh_token 可续期，但没有密码就无法在控制台用密码重登。

**方式 D：邮箱密码批量导入**

控制台「号池」→「邮箱密码导入」，每行一个 `邮箱 密码`（也支持 `邮箱----密码`）。导入时用邮箱密码**自动登录**换取 access token 与 refresh token 入池：

- 这类账号 `source=password`，**不依赖浏览器**，随时可从控制台重新登录，最稳。
- 与网页登录（`source=device`，只有 token，靠 refresh token 存活）和令牌导入（`source=token`）在控制台上以标签区分。

### 防封控与代理池

注册按**出口 IP** 被风控计数，池子让每个号从不同地址出去：

- **代理池**：控制台「号池」→「代理池」，每行一个地址（`socks5://` `socks5h://` `http://` `https://`），「检测全部」拨号回填出口 IP / 归属 / 延迟。
- **地址上限**：`signup.per_ip_limit`（默认 3）。地址达上限的代理被跳过；**代理全不可用时拒绝注册**，绝不静默走本机 IP。
- **轮换策略**：`signup.proxy_strategy` = `rotate` / `random` / `single`。
- **批量节流**：`signup.batch_max` 限制单批数量，`signup.gap_seconds` 给账号之间留间隔（串行注册，避免瞬时爆发特征）。
- 同一次注册的邮箱调用与账号调用**共用同一代理**，出口地址可归因。

### 排错：注册卡在 “setting password”，或提示 code 32 / 邮箱错误

**现象**：控制台注册长时间停在 `setting password`（可达 2–3 分钟）。

**原因**：设置密码需要**第二封邮箱验证码**，而账号服务的发码接口按地址限流。被限流时返回 `code 32`，此时邮件往往**已经发出**，程序会继续等待并重试（最多 3 次、每次间隔 20 秒，总时长受 `signup.mail_timeout_sec` 约束）。所以“卡住”多半是在等邮件，不是死锁——也可能最终成功。

**怎么解决/缓解**：
1. **换出口地址**：限流是**按 IP** 的。配上**代理池**（`use_proxies=true` + 多个代理），每个号走不同出口，可显著降低同一地址的发码频率。单个地址注册太密必然触发。
2. **放慢批量**：调大 `signup.gap_seconds`（账号间隔），降低 `signup.batch_max`（单批数量），不要短时间内连造。
3. **拉长等待**：网络/邮件服务慢时把 `signup.mail_timeout_sec` 调大（最小 10），并确认 `mail_poll_sec` 不太大。
4. **确认邮箱服务本身可用**：`mail_base` / `mail_domain` / `mail_pass` 三项要匹配你的临时邮箱 worker；passkey 错误会返回 `AUTH_ADMIN_CREDENTIAL_INVALID`（现在会给出明确中文提示）。可用“全部探测/余额”之外的方式先验证 worker 能建地址。
5. **不需要密码就跳过**：若只是要能用的号（有 refresh_token 即可续期），把 `signup.password` 留空、注册面板也留空，即跳过整个设密码步骤，注册会快很多。
6. **代理与本机出口**：国际站业务接口锁海外出口。没配代理池时注册走 `upstream.proxy`（未填则直连）；直连且本机在国内会失败或极易被风控。

### 2. 调用

```bash
curl http://127.0.0.1:4555/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"minimax-agent","messages":[{"role":"user","content":"你好"}]}'
```

### 3. 接入客户端

- **Base URL**：`http://127.0.0.1:4555/v1`
- **API Key**：留空
- **模型名**：见下方模型列表

## 模型列表

| 模型 ID | 显示名 | 上游 model | 说明 |
|---|---|---|---|
| `minimax-agent` | MiniMax Agent | `MiniMax-M3.1-Flash-Preview` | 默认对话模型 |
| `minimax-m3.1-flash` | MiniMax M3.1 Flash Preview | `MiniMax-M3.1-Flash-Preview` | 新一代 Flash 预览版，512K / 可选 1M 上下文 |
| `minimax-m3.1-flash-thinking` | MiniMax M3.1 Flash Preview Thinking | `MiniMax-M3.1-Flash-Preview` | 深度思考（thinking 由模型自身产生） |
| `minimax-m3` | MiniMax M3 | `MiniMax-M3` | 对话模式 |
| `minimax-m3-thinking` | MiniMax M3 Thinking | `MiniMax-M3` | 深度思考变体 |
| `minimax-m2.7` | MiniMax M2.7 | `MiniMax-M2.7` | 上一代对话模型 |
| `minimax-m2.7-highspeed` | MiniMax M2.7 HighSpeed | `MiniMax-M2.7-highspeed` | 上一代高速版 |

> 模型条目里的 `upstream_model` 就是直接发给直连模型 API 的 `model` 字段。上游的模型清单可实时读取：
> `GET /mavis/api/v1/models`（含 `MiniMax-M3.1-Flash-Preview`、`MiniMax-M3`、`MiniMax-M2.7`、
> `MiniMax-M2.7-highspeed`、`MiniMax-M2.5`）。thinking 由模型自身决定，不再需要单独的 variant 参数。

## API 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 服务状态、账号数、可用账号数 |
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，支持流式 |
| POST | `/v1/messages` | Anthropic Messages API，支持流式 |
| GET | `/media/{id}` | 本地化媒体文件 |
| — | `/admin/api/*` | 管理接口，供控制台调用 |

所有接口默认**不需要**携带 token。

### `/v1/chat/completions`

```bash
# 非流式
curl http://127.0.0.1:4555/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"minimax-agent","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -N http://127.0.0.1:4555/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"minimax-m3","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

### `/v1/messages`

```bash
curl http://127.0.0.1:4555/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"minimax-agent","max_tokens":1024,"system":"Be concise","messages":[{"role":"user","content":"hi"}]}'
```

设置 `"stream": true` 时返回 Anthropic SSE 事件流（含 `thinking` 内容块）。

## 命令行参数

```bash
--host HOST        监听地址（默认 127.0.0.1）
--port PORT        监听端口（默认 4555，可用环境变量 PORT 覆盖）
--data-dir DIR     SQLite 与生成媒体的存放目录（默认 ./data）
--env-file FILE    启动时读取的 dotenv 文件（默认 .env）
--reload           开发模式（代码热重载）
```

## 配置：环境变量优先

控制台能改的每一项设置都存在 SQLite 里，适合运行时调整（调度策略、冷却时间）。但**密钥**和**部署参数**不该放在数据库里——前者会明文落库并被 `/admin/api/settings` 回显，后者属于进程所处环境而非某一行数据。

因此：**环境变量始终优先，并覆盖数据库里的值**。

- 命名：`MINIMAX2API_<SECTION>__<FIELD>`（段与字段之间是**双下划线**，因为字段名里全是单下划线，如 `mail_pass`）。
- 启动时读 `.env`（可用 `--env-file` 换路径）；**真实环境变量优先于文件**，所以临时覆盖不必改文件。
- 被环境变量控制的项在控制台**只读**并标 `env`——改它不会生效，避免"改了没反应"被当成 bug。
- 启动日志会打印 `[config] environment overrides: ...`，说明哪些被环境钉住。

```bash
cp .env.example .env   # 模板，逐项有注释
```

`HOST` / `PORT` / `DATA_DIR` 也接受带前缀的写法（`MINIMAX2API_PORT` 等），裸写法仍然有效。

### 密钥脱敏

控制台与 `/admin/api/settings` 对以下**密钥字段**只回传占位符 `********`，不返回明文：

- `signup.mail_pass`（临时邮箱服务 Admin Passkey）
- `signup.password`（新账号初始密码）
- `upstream.proxy`（代理 URL，可能内嵌用户名密码）

控制台的密钥输入框默认是密码态，旁边「显示」按钮可切换明文；保存时若原样回传占位符，服务端会当作"未修改"，不会把真实密钥覆盖成 `********`。要清除某项密钥，发送空字符串。

## 账号池

### 调度策略

| 策略 | 说明 |
| --- | --- |
| `least_inflight`（默认） | 按当前打开的流数量排序；一次对话会占住账号直到流结束，比累计计数更贴近真实负载 |
| `round_robin` | 轮询 |
| `priority` | 按账号优先级降序 |
| `random` | 随机 |

所有策略共用同一套过滤：停用、已失效、冷却未到期的账号不参与调度。

### 健康状态机

| 状态 | 触发 | 恢复 |
| --- | --- | --- |
| `active` | 正常 | — |
| `cooldown` | 请求失败（含限流） | 冷却到期后自动恢复，连续失败按指数退避（60s → 900s） |
| `invalid` | 凭据被上游拒绝（401/403 或会话失效码 `1022100011`） | **仅**控制台手动「恢复」 |

账号的余额与签到状态需要查询后才有值：控制台对应列在未查询时显示「查余额」/「查签到」按钮，点击即请求上游并入库；每日签到任务也会写入这两项。

判定原则是「有错就冷却，只有凭据被拒才停用」：限流会自行恢复，给它空间即可；混淆两者会把本可恢复的容量当成永久损失。

### 请求内故障转移

单次请求最多尝试 `max_attempts` 个账号（默认 3），已试过的账号被排除。账号 `agent_id` 缺失时不计冷却、直接切换下一个；凭据被拒时立即停止并报错。

## 每日签到

默认关闭。启用后会依次请求每个账号，账号之间按 `gap_seconds` 间隔（签到是风控敏感接口，并发调用是典型的被封特征）。

- 大陆区账号自动跳过（该区无签到服务），不计为错误
- **执行顺序固定**：先调 `/config` 建立账号记录，再签到。顺序颠倒会导致当天积分被记录但永不发放
- 「已签到但未发放积分」为独立状态（`unpaid`）：签到接口无论是否发放都返回成功，只有查询积分明细才能发现异常

## 控制台

单文件 `app/static/index.html`，无 npm 构建步骤。页签：概览 / 号池 / 模型 / 签到 / 审计 / 画廊 / 设置。

## 技术细节

- **架构**：FastAPI + httpx（异步）+ SQLite（标准库 `sqlite3`，经 `asyncio.to_thread` 调度）
- **直连模型 API**：请求转发到官方客户端的模型端点 `/mavis/api/v1/llm/v1/messages`（Anthropic Messages 协议）。结构化 messages、tool use、原生 SSE 流式，无服务端会话、无回显、无重复，上下文由调用方自管
- **双协议**：`/v1/messages` 原样透传；`/v1/chat/completions` 由 `app/model_api.py` 双向转换（含流式 SSE 的 text / thinking / tool_calls 映射）
- **无状态转发**：不在服务端维护会话，每个请求独立按账号池路由
- **媒体处理**：带图请求仅传递 URL；生成图片下载至 `data/generated/` 并按 `/media/<id>` 提供
- **审计**：账号、耗时、Token 用量均落库，可按模型/结果筛选，按保留策略自动清理
- **设置热更新**：所有可调参数存于 SQLite 单 JSON 文档，修改即时生效，无需重启

## 上游协议要点

直连模型 API 只需 `Authorization: Bearer <token>`，无需签名。以下为其它不可变更项：

1. **路径**：模型端点在 `/mavis/api/v1/llm/v1/messages`（Anthropic Messages 格式）；`/chat/completions` 在该上游不可用（返回 `direct_route_not_configured`）
2. **鉴权**：仅 Bearer token；`x-mavis-*` 头可选
3. **签到签名**：签到接口另走 `x-signature` / `yy` 签名（`x-signature = md5(second + 'I*7Cf%WZ#S&%1RlZJ&C2' + body)`），查询参数顺序为签名的一部分
4. **准备顺序**：签到前必须先调用 `/config`，否则当天积分不发放

## 测试

```bash
python -m pytest tests/
```

覆盖签名配方、prompt 整形、上游回显剥离与重复回答折叠、账号池状态机、凭据类型分派（查询参数 vs Bearer）、媒体路径安全、代理池解析/选择、环境变量覆盖与密钥脱敏。

## 二开：自行添加鉴权

本项目为自用版本，接口与控制台均无鉴权。若需对外暴露，建议自行补充（两部分互相独立，均不涉及账号池与上游协议）：

- **控制台密码**：在 `app/server.py` 的 `_register()` 中为 `/admin/api/*` 挂载 FastAPI dependency 或中间件校验口令
- **调用密钥**：在 `app/gateway.py` 的 `chat_completions()` / `anthropic_messages()` 入口校验请求头，密钥存于自建表

在补齐鉴权之前，请勿将服务暴露至公网。

## 免责声明

**本项目仅用于个人学习与研究。** 与 MiniMax 无官方关联。请仅在你合法拥有账号的前提下使用，并自行承担风险。

- 本项目不提供任何形式的担保
- 使用本项目产生的任何后果由使用者自行承担
- 请勿将本项目用于任何违反相关服务条款的用途
- 请勿将本项目用于商业用途
