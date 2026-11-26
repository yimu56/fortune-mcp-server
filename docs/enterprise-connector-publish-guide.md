# WorkBuddy 企业版 · Fortune 记账 MCP 连接器 实施手册

> 适用对象：要在企业版后台**发布一个能记账的 MCP 连接器**的实施同学。
> 不需要懂 MCP 协议，只要会 Linux 命令行和后台点鼠标。
>
> 全文按“**能照着做**”的标准写：每一步都有命令、期望输出、失败分支和处理办法。
> 唯一需要提醒的是——**后台菜单名称会随版本变化**，每处均给出“按关键词找”的方法，找不到时照关键词搜即可。

---

## 目录

- [0. 开工前先看这一页](#0-开工前先看这一页)
- [1. 服务端部署（约 10 分钟）](#1-服务端部署约-10-分钟)
- [2. 三层连通性自检](#2-三层连通性自检)
- [3. 企业后台发布 MCP 连接器（核心）](#3-企业后台发布-mcp-连接器核心)
- [4. 客户端验收](#4-客户端验收)
- [5. 多用户隔离（每人只看自己的账）](#5-多用户隔离每人只看自己的账)
- [6. 运维手册](#6-运维手册)
- [7. 安全清单](#7-安全清单)
- [附录 A 对接信息卡](#附录-a-对接信息卡)
- [附录 B 命令速查](#附录-b-命令速查)
- [附录 C 排错总表](#附录-c-排错总表)

---

## 0. 开工前先看这一页

### 0.1 交付物

```
┌──────────────┐   ①部署    ┌──────────────┐   ②注册   ┌──────────────┐   ③使用
│ Fortune 记账  │ ◀──────── │ MCP Server   │ ◀──────── │ WorkBuddy    │ ◀──── 员工
│ :46031       │   HTTP     │ :18088/mcp   │  HTTPS    │ 企业版后台    │
└──────────────┘            └──────────────┘           └──────────────┘
     业务系统                  代理层              连接器 + 客户端
```

### 0.2 两个必须先确认的前提

| # | 前提 | 怎么确认 | 不满足会怎样 |
|---|---|---|---|
| 1 | **平台能访问到 MCP 服务** | 在平台侧/同网段 `curl http://<地址>:18088/healthz` 返回 `status: ok` | 后台保存连接器时一直“连接超时” |
| 2 | **每人有独立记账账号**（多用户才需要） | 让两个真人分别登录 Web 端，各自能看到独立账本 | 只能全员共享一个账套 |

> ⚠ 第 1 条最容易翻车：`fortune.example.com` 是内网地址。如果企业后台在云端，它**没有**到内网的路由。
> 三种解法：① 后台是内网私有化部署（直接填内网地址）；② 给服务一个公网域名 + HTTPS 反代 + 平台出口 IP 白名单；③ 企业网关/SASE 做映射。

### 0.3 建议的实施顺序

**先按单账号跑通全链路，再开多用户。** 顺序错了会同时排查两件事，很痛苦。

```
部署 → 自检 → 后台发布 → 客户端验收（单账号）→ 确认身份头 → 开多用户 → 双账号验收
 1        2        3              4                 5            6          7
```

---

## 1. 服务端部署（约 10 分钟）

### 1.1 上传代码

在自己电脑上（假设安装包在桌面）：

```bash
scp fortune-mcp-server.zip <服务器用户名>@fortune.example.com:/tmp/
```

登录服务器：

```bash
ssh <服务器用户名>@fortune.example.com
cd /tmp && unzip -o fortune-mcp-server.zip && cd fortune-mcp-server
```

**检查换行符**（Windows 传过去的包容易带 CRLF，会导致脚本报 `invalid option name`）：

```bash
file install.sh
# 期望：ASCII text 或 UTF-8 text
# 若是 "with CRLF line terminators"，执行：
sed -i 's/\r$//' *.sh
```

> 新版脚本已内置自愈，会自动把 CRLF 转成 LF 再跑，但先手动确认一次更稳。

### 1.2 一键安装

```bash
sudo bash install.sh
```

脚本会做 8 件事，逐条对照：

| # | 动作 | 预期输出 |
|---|---|---|
| 1 | 检查 Python ≥ 3.8 | `[+] Python: Python 3.14.3` |
| 2 | 复制程序到 `/opt/fortune-mcp` | `[+] 程序已复制到 /opt/fortune-mcp` |
| 3 | 创建运行用户 `fortune` | 无输出（正常） |
| 4 | 生成 `/etc/fortune-mcp.env`（含随机令牌） | `[+] 已生成配置 …` |
| 5 | 安装并启动 systemd 服务 | `[+] 系统服务已启动` |
| 6 | 放行 18088 端口 | `[+] firewalld 已放行 18088/tcp`（或 ufw） |
| 7 | 健康检查 | `[+] 服务已就绪 ✔` |
| 8 | 打印连接器所需信息 | 末尾的方框 |

安装完成后，**把末尾方框里的「服务地址」和「令牌」抄下来**，第 3 章要用。

### 1.3 改端口 / 跳过防火墙

```bash
sudo bash install.sh --port 18099              # 换端口
sudo bash install.sh --no-firewall             # 防火墙由别的系统统一管
sudo bash install.sh --dir /data/fortune-mcp   # 换安装目录
```

### 1.4 安装后的四个必查项

```bash
# ① 服务在跑
systemctl status fortune-mcp --no-pager
# 期望：Active: active (running)

# ② 健康检查
curl -s http://127.0.0.1:18088/healthz | python3 -m json.tool
# 期望：{"status": "ok", ..., "tools": 13, ...}

# ③ 能登录下游记账系统
set -a; . /etc/fortune-mcp.env; set +a
python3 /opt/fortune-mcp/server.py --self-check
# 期望：{"login": "ok", "defaultGroupId": 5, ...}

# ④ 协议层冒烟（含真实写入，会自动复原余额）
python3 /opt/fortune-mcp/smoke_test.py http://127.0.0.1:18088/mcp '<令牌>' --write
```

第 ④ 步的期望输出：

```
  · initialize  ok  protocol=2025-06-18 session=...
  · initialized  ok
  · tools/list  ok  共 13 个工具
  · fortune_context  ok  group=5 book=11 账户=2
  · fortune_list_accounts  ok  合计余额=100.0
  · fortune_list_categories  ok  支出分类=4
  · fortune_add_transfer  ok  余额已复原 (74.99 / 25.01)
  · 参数校验  ok  非法金额被拦截
全部通过 ✔
```

**这一步不通过就不要去后台注册**，否则后台只会返回笼统的“连接失败”。

### 1.5 部署阶段的常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `invalid option nameset: pipefail` | 包是 CRLF | `sed -i 's/\r$//' *.sh` 后重跑 |
| `未找到 python3` | 没装 Python | `sudo apt install -y python3` / `sudo yum install -y python3` |
| 健康检查未通过 | 服务起不来 | `journalctl -u fortune-mcp -n 50 --no-pager` |
| `无法连接记账服务` | 服务器到 `fortune.example.com:46031` 不通 | `curl -v http://fortune.example.com:46031/prod-api/getRsaPublicKey` |
| 端口被占 | 18088 有别的服务 | `ss -lptn 'sport = :18088'`，换 `--port` 重装 |
| 手工能跑、服务起不来 | env 没加载 | systemd 用 `EnvironmentFile`；手工跑要 `set -a; . /etc/fortune-mcp.env; set +a` |

---

## 2. 三层连通性自检

**很多人只测第一层就去后台配置，然后在第三层卡两小时。** 三层都要过。

| 层 | 从哪测 | 命令 | 期望 |
|---|---|---|---|
| ① 服务本机 | 服务器上 | `curl -s http://127.0.0.1:18088/healthz` | `status: ok` |
| ② 内网其它机器 | 办公电脑 | `curl -s http://fortune.example.com:18088/healthz` | `status: ok` |
| ③ 平台侧 | 后台“测试连接”或同网段机器 | 见下 | HTTP 200 |

第 ③ 层没有平台侧机器时，用这条模拟平台调用（**带鉴权**，这正是后台要做的事）：

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' \
  -X POST http://fortune.example.com:18088/mcp \
  -H 'Authorization: Bearer <令牌>' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

- `HTTP 200` → 平台侧能通，进第 3 章
- `HTTP 401` → 通，但令牌不对（好现象，说明网络没问题）
- 卡住/超时 → 网络不通，**先解决网络，别去后台点**

> 防火墙没放行时 `curl` 会一直挂到超时（不是立刻拒绝），这也是判断依据。

---

## 3. 企业后台发布 MCP 连接器（核心）

### 3.1 先找到入口

不同版本菜单位置不同，按下面顺序找：

| 可能的路径 | 关键词 |
|---|---|
| 管理后台 → **连接器** → 新建/接入 | `连接器`、`Connector` |
| 管理后台 → **应用与集成** → MCP 服务 | `MCP`、`集成` |
| 管理后台 → **开放能力** → 自定义连接器 | `开放平台`、`自定义` |
| 管理后台 → **智能体/助手管理** → 工具 → MCP | `工具`、`MCP Server` |

**找不到就这样搜**：后台全局搜索框输入 `MCP`，或看侧边栏是否有「连接器管理 / 工具市场 / 应用市场」。

需要 **企业管理员** 权限才能创建和发布；普通成员只能“使用”，看不到“新建”按钮。

### 3.2 第一步：基本信息

| 字段 | 填什么 | 说明 |
|---|---|---|
| 连接器类型 | **MCP Server** | 千万别选 OpenAPI / REST，两者配置方式完全不同 |
| 名称 | `Fortune 记账` | 员工在列表里看到的名字 |
| 唯一标识 / Code | `fortune-bookkeeping` | **发布后不要改**；与服务端 `serverInfo.name` 一致，便于对齐 |
| 图标 | 256×256 PNG（可选） | 没有会显示默认图 |
| 简介 | 见下方文案 | **模型靠这段判断要不要用这个连接器**，别写空话 |
| 分类 | `财务` / `办公效率` | 影响员工在市场上的检索 |

**简介建议直接用这段**（写清“能做什么”）：

```
连接 Fortune 记账系统，用自然语言记支出、记收入、账户间转账，查询账户余额、
账本、分类、收付款方与标签。支持按人隔离（每人只看到自己的账）。
```

反例（会导致 Agent 不主动调用）：
- ❌ “财务系统对接平台”
- ❌ “提供记账相关能力”
- ✅ “记一笔支出/收入、转账、查余额、查分类时使用”

### 3.3 第二步：连接与鉴权

这是最容易填错的一屏，逐字段对照：

| 字段 | 填什么 | 常见错误 |
|---|---|---|
| 传输方式 | **Streamable HTTP** | 老平台只有 SSE 选项时，填 `/sse` 地址，其余不变 |
| 服务 URL | `http://fortune.example.com:18088/mcp` | ① 漏了 `/mcp`；② 加了尾斜杠 `/mcp/` → 404 |
| 鉴权方式 | `Bearer Token` / `自定义 Header` | 选“无鉴权”会被服务端 401 |
| Header 名 | `Authorization` | 写成 `Auth` / `Token` 都不认 |
| Header 值 | `Bearer <令牌>` | ① 漏了 `Bearer ` 前缀；② 后面没空格；③ 令牌带引号/换行 |
| 超时 | `30` 秒 | 默认 5s 太短，首次登录下游约 2–4s |
| 探活路径（若有） | `/healthz` | 该路径免鉴权，适合健康检查 |

> **令牌取法**：`sudo grep MCP_AUTH_TOKEN /etc/fortune-mcp.env`，取 `=` 后面整段。

### 3.4 第三步：同步工具

保存连接配置后，点「测试连接」/「同步工具」/「刷新工具」：

**期望看到 13 个工具**（服务端开了 `MCP_DIAGNOSTIC=1` 则 14 个）：

| # | 工具 | 读/写 | 什么时候会被调用 |
|---|---|---|---|
| 1 | `fortune_context` | 只读 | 任何记账前的第一步 |
| 2 | `fortune_list_accounts` | 只读 | “查询余额” |
| 3 | `fortune_list_groups` | 只读 | 多账套场景 |
| 4 | `fortune_list_books` | 只读 | 查账本 |
| 5 | `fortune_list_categories` | 只读 | 确认分类 ID |
| 6 | `fortune_list_payees` | 只读 | 查收付款方 |
| 7 | `fortune_list_tags` | 只读 | 查标签 |
| 8 | `fortune_add_expense` | **写入** | “花了 25 吃午饭” |
| 9 | `fortune_add_income` | **写入** | “发工资 8000” |
| 10 | `fortune_add_transfer` | **写入** | “从 A 转 100 到 B” |
| 11 | `fortune_add_account` | **写入** | “加个现金账户” |
| 12 | `fortune_add_bill` | **写入** | 多分类分摊 / 关联单据 |
| 13 | `fortune_login` | 写入 | 手动刷新令牌（一般不用） |
| 14 | `fortune_diagnostic` | 只读 | **仅排障**，确认平台传了什么头 |

**勾选策略（重要）**：

| 阶段 | 勾选范围 | 目的 |
|---|---|---|
| 首轮灰度 | 只勾 1–7（7 个只读） | 验证链路，零风险 |
| 二轮 | 加 8、9、10 | 放开记账，观察一周 |
| 三轮 | 加 11、12 | 全功能 |
| 排障时 | 临时勾 14 | 会暴露请求头，用完关掉 |

如果后台有「只读 / 需确认 / 高风险」标记，把 8–13 全部标为**需确认**。

**不要修改工具描述**。模型完全靠描述判断“何时调用、怎么传参”，例如 `fortune_add_expense` 的描述里写了“用户说花了 25 块吃午饭时调用”，改成“新增支出记录”会明显降低命中率。

### 3.5 第四步：可见范围与授权

| 项 | 首轮建议 | 说明 |
|---|---|---|
| 可见范围 | 指定 **1 个测试部门** 或 **2–3 个测试账号** | 不要一上来全员可见 |
| 授权模式 | 平台统一授权（服务端固定账号） | 单账号阶段用它 |
| 数据说明 | 注明“测试账本” | 避免员工误以为是真实财务数据 |
| 多用户隔离 | **先不开** | 等第 5 章确认身份头后再开 |

### 3.6 第五步：测试连接

保存后回到列表，找到刚建的连接器：

1. 点 **「测试」/「校验」/「调试」**（有的话）
2. 期望：能拉到工具列表，或提示“连接成功”
3. 失败时对照下表

| 报错 | 原因 | 处理 |
|---|---|---|
| 连接超时 / 无法访问 | 网络不通（第 2 章第 ③ 层） | 先解决网络；查防火墙、安全组、路由 |
| **401 Unauthorized** | 令牌错 / 少了 `Bearer ` | 重取令牌；确认值是 `Bearer xxx` |
| **404 Not Found** | URL 少了 `/mcp` 或有尾斜杠 | 用 `http://host:18088/mcp` |
| **405 Method Not Allowed** | 平台用 GET 探测 `/mcp` | 正常（本服务 POST 才处理）；若平台强制 GET 握手，改 `/sse` |
| 工具列表为空 | 没点同步 / 全部未勾选 | 重新“同步工具”，确认勾选 |
| `TLS handshake failed` | 用了 https 但服务是 http | 改 http，或按 `deploy/nginx-*.conf` 配证书 |

### 3.7 第六步：发布

| 路径 | 适用 | 操作 | 审核 |
|---|---|---|---|
| **A. 私有发布** | 公司内部自用 | 点「发布」/「上线」，仅对可见范围生效 | 一般无需审核 |
| **B. 公开上架** | 对外分发 / 上市场 | 另建公开资产 → 补隐私说明、数据流转、资质 → 提审 | 人工+机器，约 1 周 |

私有版本的静态 Bearer Token **通常不允许直接上架公开市场**；对外分发需单独走公开资产并实现 OAuth 授权。

发布后确认三件事：

- [ ] 连接器状态 = **已发布 / 已启用**
- [ ] 可见范围包含测试账号
- [ ] 调用日志 / 审计开关已打开

### 3.8 这一屏的“坑位地图”

```
┌─ 基本信息 ────────────────────────────────┐
│ 类型：必须 MCP（不是 OpenAPI）             │ ← 选错整页配置都白填
│ 标识：发布后不可改                          │
└───────────────────────────────────────────┘
┌─ 连接配置 ────────────────────────────────┐
│ URL：http://fortune.example.com:18088/mcp         │ ← 漏 /mcp、加尾斜杠 → 404
│ 鉴权：Bearer <token>                      │ ← 少 "Bearer " 前缀 → 401
│ 超时：30s                                 │ ← 默认 5s 会偶发失败
└───────────────────────────────────────────┘
┌─ 工具 ────────────────────────────────────┐
│ 13 个（诊断开启则 14）                     │ ← 少于 13 说明没同步成功
│ 首轮只勾只读                              │
└───────────────────────────────────────────┘
┌─ 可见范围 ────────────────────────────────┐
│ 先 2–3 人，别全员                          │
└───────────────────────────────────────────┘
```

---

## 4. 客户端验收

用测试账号登录 WorkBuddy 桌面端：

1. 侧边栏 → **「专家 · 技能 · 连接器」→「连接器」**
2. 找到 `Fortune 记账`，点 **「+」/「添加」**
3. 首次会提示 **「此 MCP 服务器需要信任」** → 点 **信任**
4. 卡片出现 **绿点**，显示 **13/13 工具已启用**（只勾只读则 7/7）

### 4.1 八条验收话术

按顺序说，每条都要命中对应工具：

| # | 测试指令 | 期望调用的工具 | 期望结果 |
|---|---|---|---|
| 1 | “查看记账账户和余额” | `fortune_context` | 列出账户 + 余额 |
| 2 | “有哪些支出分类” | `fortune_list_categories` | 维持类/消费类/提升类/社交类 |
| 3 | “记一笔支出，午饭 25，用现金，消费类” | `fortune_add_expense` | 余额减 25 |
| 4 | “这个月发了 8000 工资” | `fortune_add_income` | 余额加 8000 |
| 5 | “从现金转 100 到工资卡” | `fortune_add_transfer` | 两账户余额相应变化 |
| 6 | “加一个支付宝账户，里面有 500” | `fortune_add_account` | 新账户出现 |
| 7 | “记一笔 -5 块的支出” | 被参数校验拦下 | 可读错误，**不是** 500 |
| 8 | “记到账本 13、账户 15 上” | 被归属校验拦下 | “账本/账户不属于当前用户” |

### 4.2 验收清单（逐项打勾）

- [ ] `/healthz` 返回 ok 且 `tools=13`
- [ ] 服务端 `smoke_test.py --write` 全部通过
- [ ] 后台“测试连接”成功，工具列表 13 个
- [ ] 客户端卡片绿点，工具数正确
- [ ] 1–6 号话术全部命中正确工具
- [ ] 7、8 号异常场景被拦并给出可读错误
- [ ] 后台审计日志能看到每次 `tools/call`
- [ ] 服务端 `journalctl -u fortune-mcp` 有 `audit user=... tool=... ok`

---

## 5. 多用户隔离（每人只看自己的账）

### 5.1 先搞清楚要解决什么

默认形态：服务端只持有一组 `.env` 凭据 → **所有人看到同一份数据**。

隔离必须发生在**凭据解析层**：每个请求先认出“这是谁”，再取“这个人的记账账号”。靠提示词、或让用户自己报账号都做不到（随便报个名字就串账）。

三种实现方式：

| 模式 | 原理 | 前提 | 改造 | 运维成本 |
|---|---|---|---|---|
| **A 身份透传 + 多租户**（推荐） | 平台把当前用户标识放请求头，服务端查映射表取凭据 | 平台支持**用户身份透传** | 已内置 | 加人一行命令 |
| **B 一人一实例** | 每人一个端口 + 一个连接器 | 无 | 无需改代码 | 人数 × 配置 |
| **C 本地 stdio** | 每人自己电脑跑，用自己账号 | 允许成员加自定义连接器 | 无需改代码 | 分散，难审计 |

选型：**人数 ≥ 20 选 A；平台不透传身份且 ≤ 20 人选 B；重隐私选 C。**

### 5.2 模式 A：完整操作步骤

#### 前置：确认平台确实透传了身份（**别跳过**）

这是 A 能否成立的唯一判据，用诊断工具 5 分钟测出来：

```bash
# ① 打开诊断工具并重启
sudo sed -i 's/^MCP_DIAGNOSTIC=.*/MCP_DIAGNOSTIC=1/' /etc/fortune-mcp.env
sudo systemctl restart fortune-mcp

# ② 回后台连接器点「同步工具」，应从 13 个变成 14 个
# ③ 在客户端里说：调用 fortune_diagnostic 看看
```

返回里重点看三个字段：

| 字段 | 含义 | 判断 |
|---|---|---|
| `headers` | 本次请求的全部请求头 | 有没有 `X-User-Id` / `X-WorkBuddy-User` / `X-Employee-Id` |
| `identityHeaders` | 命中的身份头 | **非空 = A 可行** |
| `platformUser` | 服务端解析出的用户 | 应等于工号/邮箱前缀 |

- `identityHeaders` 有值 → **继续往下做**
- 为空，但 `headers` 里有别的不认识的头 → 把头名加进 `MCP_USER_HEADERS` 即可
- 完全是空的（只有固定的 `Authorization` 和 `UA`）→ **A 走不通，跳到 5.3 走 B**

测完关掉：

```bash
sudo sed -i 's/^MCP_DIAGNOSTIC=.*/MCP_DIAGNOSTIC=0/' /etc/fortune-mcp.env
sudo systemctl restart fortune-mcp
```

#### 第一步：给每个人建记账账号

在 Fortune 记账系统后台给每人开账号（或走 LDAP/SSO 同步）。
**判据**：让两个人各自登录 Web 端，各自能看到独立账本（不同 groupId）。

#### 第二步：建“平台用户 → 记账账号”映射

**方式一：交互式菜单（推荐，不用记参数）**

```bash
sudo python3 /opt/fortune-mcp/useradmin.py menu
```

屏幕会长这样，输入数字选择即可：

```
==========================================================
 Fortune 记账 · 多用户管理
 凭据库：/var/lib/fortune-mcp/users.json
 当前 2 个用户：demo_user2、demo_user
==========================================================
  1) 添加 / 修改用户
  2) 查看全部用户
  3) 停用 / 启用用户
  4) 删除用户
  5) 验证某账号能否登录
  6) CSV 批量导入
  0) 退出
```

- 选 `1` → 逐项问：平台用户 → 记账账号 → 密码（不回显）→ 接口地址（回车用默认）→ 备注，
  最后**列出摘要待确认**，确认后才写盘，误填可以直接放弃。
- 保存后可以**立即验证**：脚本会用刚存的凭据真登一次记账系统，给出
  `✔ 登录成功：分组 6（示例用户）、账本「示例用户的默认账本」、账户 1 个`——
  密码打错当场就能发现，不用等员工反馈。
- 加完一个人会问“继续添加下一个？”，批量录 10 个人不用反复敲命令。

**方式二：只加一个人（向导）**

```bash
sudo python3 /opt/fortune-mcp/useradmin.py add
```

等价于菜单里的 `1`，只走一遍添加流程，加完即退出。

**方式三：脚本化（批量/自动化/CI）**

```bash
echo '示例用户的记账密码' | sudo python3 /opt/fortune-mcp/useradmin.py add \
    --user demo_user \
    --fortune-user 示例用户 \
    --note '示例用户/研发部' \
    --password-stdin
```

> 管道传密码是为了不让密码进 shell 历史。加 `--verify` 可在保存后立即验证。
> 非交互环境（如 CI）里如果漏了 `--user` / `--fortune-user`，脚本会直接报错并提示改用 `menu`，
> 不会傻等输入。

三个参数别搞混：

| 参数 | 含义 | 取值来源 | 例子 |
|---|---|---|---|
| `--user` | **平台侧**的用户标识 | 诊断工具 `platformUser` 的返回值 | `demo_user`、`10086`、`user@example.com` |
| `--fortune-user` | **记账系统**的登录名 | 记账系统里的账号 | `示例用户` |
| `--password-stdin` | 密码从标准输入读 | — | — |

> ⚠ 最常见的错误：`--user` 填成了记账账号名。它必须是**平台实际传过来的那个值**（工号/邮箱前缀），否则永远匹配不上，员工会看到“还没有绑定”。

批量导入（20 人以上推荐）：

```bash
# 1) 生成模板
sudo python3 /opt/fortune-mcp/useradmin.py menu               # 交互式菜单（推荐）
sudo python3 /opt/fortune-mcp/useradmin.py add                # 向导式添加
sudo python3 /opt/fortune-mcp/useradmin.py add --user x --fortune-user y --verify --password-stdin
sudo python3 /opt/fortune-mcp/useradmin.py template > users.csv

# 2) 编辑（列：platform_user,fortune_username,password,note）
#    demo_user,示例用户,CHANGE_ME,示例用户/研发部
#    demo_user2,示例用户2,CHANGE_ME,示例用户2/市场部

# 3) 导入
sudo python3 /opt/fortune-mcp/useradmin.py import users.csv
# 期望：导入完成：新增 2，更新 0，失败 0

# 4) 核对
sudo python3 /opt/fortune-mcp/useradmin.py list
```

导入完**立即删掉 CSV**（里面有明文密码）：`rm -f users.csv`。

#### 第三步：打开多租户开关

```bash
sudo sed -i 's/^#*\s*FORTUNE_MULTI_TENANT=.*/FORTUNE_MULTI_TENANT=1/' /etc/fortune-mcp.env
sudo systemctl restart fortune-mcp
journalctl -u fortune-mcp -n 20 --no-pager | grep 多租户
# 期望：[...] 多租户模式：凭据库 /var/lib/fortune-mcp/users.json（已绑定 2 人）
#       [...] 身份头优先级：X-WorkBuddy-User > X-User-Id > ...
```

#### 第四步：双账号联调（关键）

找两个真人，用**各自的 WorkBuddy 账号**，同时问同一句话：

| 人 | 说 | 期望 |
|---|---|---|
| 示例用户 | “查看账户和余额” | 只看到示例用户的账户 |
| 示例用户2 | “查看账户和余额” | 只看到示例用户2的账户（**不能看到示例用户的**） |
| 示例用户 | “记一笔午饭 25” | 示例用户余额变化，示例用户2那边不变 |

服务端日志应能对上：

```
audit user=demo_user fortune=示例用户 tool=fortune_context ok
audit user=demo_user2     fortune=示例用户2 tool=fortune_context ok
audit user=demo_user fortune=示例用户 tool=fortune_add_expense ok
```

如果日志里 `user=` 都是同一个值 → 平台传的是固定值而非逐人身份，A 不成立，转 B。

#### 第五步：异常场景必须验（别省）

| 场景 | 怎么造 | 期望 |
|---|---|---|
| 未绑定用户 | 用一个没导入映射的账号登录 | “还没有绑定记账账号” + 给出 `useradmin.py add` 命令 |
| 已停用用户 | `useradmin.py disable --user demo_user2` | “账号已被停用，请联系管理员” |
| 越权传别人的账户 ID | “记到账户 15 上”（别人的账户） | “账户 15 不属于当前用户；可用账户：现金(14)” |
| 越权传别人的账本 ID | 高级接口传 `book_id=13` | “账本 13 不属于当前用户的当前分组” |

> 这四条不是走过场。实测发现下游记账系统**不校验账户是否属于该账本**——代理层不拦就能扣别人的钱。校验已内置，但每次升级后建议抽测一遍。

### 5.3 模式 B：一人一实例（平台不透传身份时）

原理：每人一个端口 + 一份配置 + 后台一个连接器。

多实例的坑：`install.sh` 默认 env 固定为 `/etc/fortune-mcp.env`、服务名固定 `fortune-mcp`，
直接跑第二遍会**互相覆盖**。批量加人用随包提供的脚本：

```bash
# 语法：sudo bash add-tenant.sh <英文名> <记账账号> <密码> [端口]
sudo bash deploy/add-tenant.sh demo_user 示例用户 'CHANGE_ME' 18091
sudo bash deploy/add-tenant.sh demo_user2     示例用户2 'CHANGE_ME' 18092
```

脚本会自动完成：建 `fortune-mcp-<名>` 运行用户 → 复制程序到 `/opt/fortune-mcp-<名>`
→ 派生独立的 systemd 单元与 `/etc/fortune-mcp-<名>.env`（换账号、换端口、换缓存目录）
→ 启动并健康检查 → 打印下一个连接器要填的地址和令牌位置。

然后回后台，给每人建一个连接器（名称必须带真实姓名，如「记账-示例用户」），可见范围只勾本人。

**这套方案的固有缺陷**（讲清楚再决定）：
- 员工需要在连接器列表里选择对应实例，挑错就记到同事账上；
- N 人 = N 个端口、N 个服务、N 份配置，升级要循环 N 次；
- 日志分散，出问题挨个查。

### 5.4 模式 C：本地 stdio 连接器

适合：员工电脑可控、重隐私、能接受失去集中审计。

每人在自己电脑的 `~/.workbuddy/mcp.json`（Windows：`C:\Users\<用户名>\.workbuddy\mcp.json`）写：

```json
{
  "mcpServers": {
    "fortune-bookkeeping": {
      "command": "python3",
      "args": ["/opt/fortune-mcp/server.py", "--stdio"],
      "env": {
        "FORTUNE_BASE_URL": "http://fortune.example.com:46031/prod-api",
        "FORTUNE_USERNAME": "示例用户",
        "FORTUNE_PASSWORD": "CHANGE_ME"
      }
    }
  }
}
```

Windows 上把 `python3` 换成 `python`、路径换成 `D:\\tools\\fortune-mcp\\server.py`（双反斜杠）。

改完重启客户端 → 连接器 → 自定义连接器 → 点“信任”。

> 前提：① 企业版允许成员添加自定义连接器（管理员侧有开关）；② 员工电脑能装 Python；③ `fortune.example.com:46031` 对员工电脑可达。

### 5.5 多用户相关常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 提示“还没有绑定记账账号” | `--user` 的值和平台传的不一致 | 用 `fortune_diagnostic` 看 `platformUser`，按它重建映射 |
| 所有人看到同一份数据 | `FORTUNE_MULTI_TENANT` 还是 0 | 改 1 并 `systemctl restart` |
| 提示“未能识别当前用户身份” | 平台没传身份头 | 看诊断 `headers`；没有就走 B/C |
| 改了映射没生效 | 改的是另一个 users.json | 确认 `FORTUNE_USERS_FILE` 与实际编辑的文件一致 |
| 导入 CSV 报“表头需包含” | 列名不对 | 用 `useradmin.py template` 生成，别手写 |
| 映射里有这个用户但仍报未绑定 | 用户被 disable 了 | `useradmin.py list` 看状态 |

---

## 6. 运维手册

### 6.1 升级

```bash
# 1) 备份（配置 + 凭据库 + 密钥，三件缺一不可）
sudo tar czf /root/fortune-backup-$(date +%F).tgz \
    /etc/fortune-mcp.env /var/lib/fortune-mcp/users.json /var/lib/fortune-mcp/users.json.key

# 2) 覆盖程序文件（不要覆盖 env 和 users.json）
sudo cp server.py fortune_api.py fortune_tools.py user_store.py useradmin.py /opt/fortune-mcp/
sudo chown -R fortune:fortune /opt/fortune-mcp

# 3) 重启并回归
sudo systemctl restart fortune-mcp
python3 /opt/fortune-mcp/smoke_test.py http://127.0.0.1:18088/mcp '<令牌>'
```

**回滚**：覆盖回旧文件 → 重启。后台连接器不用动（URL 和 Token 没变）。

### 6.2 日常检查

```bash
systemctl is-active fortune-mcp
curl -s localhost:18088/healthz | python3 -m json.tool
sudo python3 /opt/fortune-mcp/useradmin.py list
journalctl -u fortune-mcp --since "1 hour ago" | grep audit | tail -20
```

### 6.3 审计日志

每次工具调用打一行：

```
audit user=demo_user fortune=示例用户 tool=fortune_add_expense ok
audit user=demo_user fortune=示例用户 tool=fortune_add_expense fail=账户 15 不属于当前用户；可用账户：现金(14)
```

- 查某人做了什么：`journalctl -u fortune-mcp | grep "user=demo_user"`
- 查失败：`journalctl -u fortune-mcp | grep "fail="`
- 建议接入日志平台，保留 ≥ 6 个月

### 6.4 轮换令牌

```bash
sudo sed -i "s/^MCP_AUTH_TOKEN=.*/MCP_AUTH_TOKEN=$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)/" /etc/fortune-mcp.env
sudo systemctl restart fortune-mcp
sudo grep MCP_AUTH_TOKEN /etc/fortune-mcp.env     # 取新值
# 立刻更新后台连接器的 Header —— 中间会短暂 401
```

### 6.5 卸载

```bash
sudo bash uninstall.sh --dry-run          # 先预览，不动手
sudo bash uninstall.sh                    # 停服务 + 删程序，保留配置与凭据库
sudo bash uninstall.sh --yes --purge      # 彻底清除（配置、凭据库、运行用户、防火墙）
```

卸载后**务必回后台把连接器停用/下架**，否则平台会一直重连并报超时。

> 多实例（模式 B）场景：`uninstall.sh` 只处理默认实例 `fortune-mcp`。其余实例需逐个清理：`sudo systemctl disable --now fortune-mcp-<名>` + `sudo rm -rf /opt/fortune-mcp-<名> /etc/fortune-mcp-<名>.env`。

---

## 7. 安全清单

上线前逐条确认：

- [ ] 服务只在内网或受控入口暴露，公网入口必须 HTTPS
- [ ] 平台侧出口 IP 白名单已配置
- [ ] `MCP_AUTH_TOKEN` ≥ 32 位随机，不是 `123456`
- [ ] `/etc/fortune-mcp.env` 权限 600
- [ ] `users.json` 与 `users.json.key` 权限 600，且**分开备份**
- [ ] 首轮只勾只读工具，写入工具标“需确认”
- [ ] 审计日志已接入或至少本地留存
- [ ] 已实测四条越权/异常场景（未绑定、已停用、别人账户、别人账本）
- [ ] 离职流程包含：`useradmin.py disable --user xxx`（先停用留痕，再删除）
- [ ] 下游业务系统自身的权限校验没有被绕过

> 特别说明：**不要依赖下游系统做归属校验**。实测 Fortune 的 `/fortune/bill/add` 不校验“账户是否属于该账本”，只靠它拦不住越权。本服务已在代理层补齐账户/账本/分类/标签/收付款方的归属校验，但每次接入新版本后建议抽测。

---

## 附录 A 对接信息卡

可直接转给平台/安全同事：

```
系统名称：Fortune 记账（测试环境）
连接器标识：fortune-bookkeeping
协议：MCP 2025-06-18（兼容 2025-03-26 / 2024-11-05）
传输：Streamable HTTP
入口：http://fortune.example.com:18088/mcp        （SSE 备选：http://fortune.example.com:18088/sse）
探活：http://fortune.example.com:18088/healthz    （免鉴权，返回 status=ok）
鉴权：Authorization: Bearer <MCP_AUTH_TOKEN>
工具数：13（7 只读 / 6 写入）；诊断开启时 14
下游依赖：http://fortune.example.com:46031/prod-api（同主机，端口 46031）
运行方式：systemd 服务 fortune-mcp，用户 fortune，监听 18088/tcp
数据范围：测试账套，无真实个人金融数据
多用户：支持，按请求头识别（默认 X-WorkBuddy-User > X-User-Id > X-Employee-Id）
日志：journalctl -u fortune-mcp（调用摘要，不含密码与令牌）
```

---

## 附录 B 命令速查

```bash
# —— 部署 ——
sudo bash install.sh                       # 一键安装
sudo bash install.sh --port 18099          # 换端口
sudo bash install.sh --no-firewall         # 不动防火墙

# —— 自检 ——
curl -s localhost:18088/healthz
python3 /opt/fortune-mcp/server.py --self-check
python3 /opt/fortune-mcp/smoke_test.py http://127.0.0.1:18088/mcp '<TOKEN>' --write

# —— 服务管理 ——
sudo systemctl restart|status|stop fortune-mcp
journalctl -u fortune-mcp -f
journalctl -u fortune-mcp --since "1 hour ago" | grep audit

# —— 多用户 ——
echo '密码' | sudo python3 /opt/fortune-mcp/useradmin.py add --user <平台用户> --fortune-user <记账账号> --password-stdin
sudo python3 /opt/fortune-mcp/useradmin.py list
sudo python3 /opt/fortune-mcp/useradmin.py disable --user <平台用户>
sudo python3 /opt/fortune-mcp/useradmin.py remove  --user <平台用户>
sudo python3 /opt/fortune-mcp/useradmin.py template > users.csv
sudo python3 /opt/fortune-mcp/useradmin.py import users.csv

# —— 卸载 ——
sudo bash uninstall.sh --dry-run
sudo bash uninstall.sh --yes --purge
```

---

## 附录 C 排错总表

| 现象 | 大概率原因 | 处理 |
|---|---|---|
| `invalid option nameset: pipefail` | 脚本 CRLF | `sed -i 's/\r$//' *.sh` |
| 后台“连接超时” | 平台到服务网络不通 | 第 2 章第 ③ 层；查防火墙/安全组/路由 |
| 后台 **401** | 令牌错 / 少 `Bearer ` | 重取令牌，确认 `Bearer xxx` |
| 后台 **404** | URL 少了 `/mcp` | `http://host:18088/mcp`，无尾斜杠 |
| 后台 **405** | 平台 GET 探测 `/mcp` | 正常；必要时改 `/sse` |
| 工具列表 0 个 | 未同步 / 未勾选 | 重新同步，勾选工具 |
| 客户端灰点 | 未点“信任” | 重新信任，重启客户端 |
| Agent 不调工具 | 工具描述被改 / 简介空泛 | 恢复描述，简介写清“能做什么” |
| 调用返回 `无法连接记账服务` | 服务器到 `:46031` 不通 | `curl http://fortune.example.com:46031/prod-api/getRsaPublicKey` |
| 调用返回 `HTTP 500 / 接口错误` | 下游校验失败 | 错误原文在 `content.text` 的 `msg` 里 |
| SSE 一直无响应 | 代理缓冲 | nginx 加 `proxy_buffering off` |
| 会话频繁失效 | 多实例无粘滞 / TTL 到期 | 单实例，或调大 `MCP_SESSION_TTL` |
| 所有人看到同一份数据 | 多租户未开 | `FORTUNE_MULTI_TENANT=1` 后重启 |
| “还没有绑定记账账号” | `--user` 与平台传值不一致 | 用 `fortune_diagnostic` 核对 `platformUser` |
| “未能识别当前用户身份” | 平台不透传身份 | 走模式 B 或 C |
