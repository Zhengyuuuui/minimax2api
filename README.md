# minimaxcode2api

> MiniMax Agent 多账号反代网关 — 账号池调度、设备指纹对齐官方客户端、双认证路径（Web JWT / 设备流 OAuth）、浏览器一键登录、支持 OpenAI / Anthropic 协议与标准客户端。

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
- **每日签到**：多账号自动签到、积分核查、大陆区账号自动跳过
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
│   ├── signin.py          # 每日签到与积分核查
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

| 模型 ID | 显示名 | 类型 | 说明 |
|---|---|---|---|
| `minimax-agent` | MiniMax Agent | chat | 通用 Agent，自动规划并调用工具 |
| `minimax-m3` | MiniMax M3 | chat | 对话模式，响应更快 |
| `minimax-m3-thinking` | MiniMax M3 Thinking | chat | 深度思考，推理内容走 `reasoning_content` |
| `minimax-m2.7` | MiniMax M2.7 | chat | 上一代对话模型 |
| `minimax-m2.7-highspeed` | MiniMax M2.7 HighSpeed | chat | 上一代高速版 |
| `minimax-image` | MiniMax Image | image | 图像生成，图片以 Markdown 返回 |

> 所有 chat 模型到达的是同一个上游 agent；模型条目仅决定 `upstream_model` 字段的取值。

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
--reload           开发模式（代码热重载）
```

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
- **无状态转发**：一次请求建一个上游会话，不跨请求复用，避免不同调用方上下文串扰
- **媒体处理**：上游附件需其自有上传接口签名，无法复现，故带图请求仅传递 URL；生成图片下载至 `data/generated/` 并按 `/media/<id>` 提供
- **审计**：请求体、响应体、账号、耗时、Token 估算均落库，可按模型/结果筛选，按保留策略自动清理
- **设置热更新**：所有可调参数存于 SQLite 单 JSON 文档，修改即时生效，无需重启

## 上游协议要点

以下为不可变更项，改动会直接导致请求被拒且上游不指明字段：

1. **签名算法**：`x-signature = md5(second + 'I*7Cf%WZ#S&%1RlZJ&C2' + body)`；`yy = md5(encodeURIComponent(target) + '_' + body + md5(ms) + 'ooui')`
2. **查询参数顺序**：agent 22 项、签到 22 项，顺序为签名的一部分，禁止排序
3. **时钟一致性**：URL 中的 `unix` 与 `yy` 的 `ms` 必须来自同一次读数
4. **编码差异**：agent 路径用 `encodeURIComponent`（空格 `%20`）；签到路径用 form 编码（空格 `+`）
5. **签到签名目标**：相对路径，且签名 URL 含 `op_ticket=undefined`，实际发出的请求不含该参数
6. **准备顺序**：签到前必须先调用 `/config`，否则当天积分不发放
7. **主机区分**：会话接口位于 `agent-stream.<domain>`，其余接口位于 `agent.<domain>`

## 测试

```bash
python -m pytest tests/
```

覆盖签名配方、prompt 整形、账号池状态机、凭据类型分派（查询参数 vs Bearer）、媒体路径安全。

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
