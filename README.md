# Fortune 记账 MCP Server

把 Fortune 记账系统（RuoYi 风格 `prod-api`）封装成**可远程部署的 MCP 服务**，供 WorkBuddy 企业版连接器调用。
纯 Python 标准库实现，**零第三方依赖**，单机常驻、内存占用 < 60 MB。

- 传输协议：**Streamable HTTP**（推荐，`POST /mcp`）+ **旧版 HTTP+SSE**（兼容）
- 同时支持 **stdio** 模式（本地 `mcp.json` 调试）
- 工具数：**13 个**（查询账户/账本/分类/标签/收付款方 + 记账/转账/建账户）
- 默认下游地址：`http://fortune.example.com:46031/prod-api`（可用 `FORTUNE_BASE_URL` 覆盖）
- 当前版本：**v3.1.2**（与 `SERVER_VERSION`、Docker 镜像标签及发布标签一致）

---

## 一、目录结构

```
fortune-mcp-server/
├── server.py                     # 服务入口：HTTP + SSE + stdio
├── fortune_api.py                # 记账系统 HTTP 客户端（含手写 RSA 加密登录）
├── fortune_tools.py              # 13 个 MCP 工具定义与实现
├── user_store.py                 # 多租户凭据库（加密存储 平台用户→记账账号）
├── useradmin.py                  # 凭据库管理 CLI（add/list/disable/import）
├── smoke_test.py                 # 冒烟测试（可在服务器上直接跑）
├── healthcheck.py                # 容器健康检查
├── install.sh                    # 一键安装（systemd / 用户级服务）
├── uninstall.sh                  # 卸载（支持保留配置/彻底清除/dry-run）
├── .env.example                  # 配置样例
├── Dockerfile / docker-compose.yml
├── deploy/
│   ├── add-tenant.sh             # 「一人一实例」批量加人脚本
│   ├── fortune-mcp.service       # systemd 单元参考
│   └── nginx-fortune-mcp.conf    # HTTPS 反代参考（SSE 安全配置）
├── README.md                     # 本文
├── CHANGELOG.md                  # 版本变更记录
├── LICENSE                       # MIT 许可（Copyright (c) 2026 yimu56）
├── .gitignore                    # 忽略本地配置与缓存
└── docs/enterprise-connector-publish-guide.md    # 实施手册：部署→后台发布→验收→多用户→运维
```

---

## 二、5 分钟部署（Linux）

### 1. 上传并安装

在**能访问记账系统的 Linux 服务器**上执行：

```bash
# 方式一：scp 上传整个目录
scp -r fortune-mcp-server user@fortune.example.com:/tmp/
ssh user@fortune.example.com
cd /tmp/fortune-mcp-server && sudo bash install.sh

# 方式二：已在服务器上，直接解压执行
unzip fortune-mcp-server.zip && cd fortune-mcp-server && sudo bash install.sh
```

脚本会自动完成：环境检查 → 复制到 `/opt/fortune-mcp` → 创建 `fortune` 运行用户 →
生成 `/etc/fortune-mcp.env`（含**随机 Bearer Token**）→ 安装 systemd 服务并启动 → 放行防火墙端口 →
健康检查 → 打印连接器所需信息。

常用参数：

| 参数 | 说明 |
|---|---|
| `--port 18088` | 改监听端口（默认 18088） |
| `--dir /opt/fortune-mcp` | 改安装目录 |
| `--no-firewall` | 不改防火墙规则 |
| `--uninstall` | 等同执行 `uninstall.sh`，可追加 `--yes` / `--purge` / `--dry-run` |

### 2. 手工部署（不想用脚本）

```bash
sudo useradd -r -s /sbin/nologin -d /var/lib/fortune-mcp fortune
sudo mkdir -p /opt/fortune-mcp /var/lib/fortune-mcp
sudo cp server.py fortune_api.py fortune_tools.py user_store.py useradmin.py \
        smoke_test.py healthcheck.py uninstall.sh /opt/fortune-mcp/
sudo chmod +x /opt/fortune-mcp/uninstall.sh
sudo cp .env.example /etc/fortune-mcp.env
sudo vi /etc/fortune-mcp.env          # 改 MCP_AUTH_TOKEN、确认账号密码
sudo cp deploy/fortune-mcp.service /etc/systemd/system/
sudo sed -i "s#/usr/bin/python3#$(command -v python3)#" /etc/systemd/system/fortune-mcp.service
sudo chown -R fortune:fortune /opt/fortune-mcp /var/lib/fortune-mcp
sudo chmod 600 /etc/fortune-mcp.env
sudo systemctl daemon-reload && sudo systemctl enable --now fortune-mcp
```

### 3. Docker 部署

```bash
cp .env.example .env && vi .env        # 至少改 MCP_AUTH_TOKEN
docker compose up -d
docker compose logs -f
```

### 4. 验证

```bash
# 健康检查（不需要 token）
curl -s http://127.0.0.1:18088/healthz | python3 -m json.tool

# 登录下游系统自检
set -a; . /etc/fortune-mcp.env; set +a
python3 /opt/fortune-mcp/server.py --self-check

# 完整协议冒烟测试（初始化 / 工具列表 / 查询 / 写入并复原余额）
python3 /opt/fortune-mcp/smoke_test.py http://127.0.0.1:18088/mcp '<TOKEN>' --write
```

期望输出：

```
  · initialize  ok  protocol=2025-06-18 session=...
  · tools/list  ok  共 13 个工具
  · fortune_context  ok  group=5 book=11 账户=2
  · fortune_add_transfer  ok  余额已复原 (74.99 / 25.01)
全部通过 ✔
```

---

## 三、卸载

用 `uninstall.sh`，默认**保守**：只停服务、删单元、删程序目录，**保留** `/etc/fortune-mcp.env`（含账号与令牌）和 `/var/lib/fortune-mcp`（token 缓存），方便排查或重装。

```bash
# 1) 先预览，不动任何东西
sudo bash uninstall.sh --dry-run

# 2) 交互确认后卸载（保留配置与缓存）
sudo bash uninstall.sh

# 3) 无人值守（脚本化）
sudo bash uninstall.sh --yes

# 4) 彻底清除：配置 + 缓存 + 运行用户 + 防火墙端口
sudo bash uninstall.sh --yes --purge
```

| 参数 | 作用 |
|---|---|
| `--yes` / `-y` | 跳过确认（非交互执行必须加） |
| `--purge` | 删配置、缓存、`fortune` 用户，并关闭防火墙端口 |
| `--keep-app` | 保留程序目录，只停服务（升级前临时停服用） |
| `--close-firewall` | 只关闭放行的端口 |
| `--dry-run` | 只打印将要执行的动作 |
| `--scope system\|user\|auto` | 指定服务作用域，默认自动判断 |
| `--dir <路径>` | 装在非默认目录时指定 |
| `--port <端口>` | 非默认端口时指定（清理防火墙/残留进程用） |

脚本会自动处理四种安装形态：**systemd 系统服务**、**systemd 用户级服务**、**Docker Compose**、**裸进程**（按 `server.py` 命令行精确匹配，不会误杀其他进程）。

> 安装时 `uninstall.sh` 会被一并复制到 `/opt/fortune-mcp/`，所以即使安装包被清理，也能直接 `sudo bash /opt/fortune-mcp/uninstall.sh`（脚本会先把自己搬到 `/tmp` 再执行，不会在删除目录时把自己删掉）。
>
> 用户级安装的配置在 `$APP_DIR/.env`，删目录前会自动备份为 `$APP_DIR.env.bak`，不会静默丢失。
> 卸载后记得去企业后台把连接器**停用/下架**，否则平台侧仍会反复重连并报超时。

也可以直接用安装脚本的快捷入口：`sudo bash install.sh --uninstall`（内部转调 `uninstall.sh`）。

---

## 四、对外提供的端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/mcp` | **Streamable HTTP**：JSON-RPC 主入口（企业连接器填这个） |
| GET | `/mcp` | 返回 405（本服务不提供服务端推送流） |
| DELETE | `/mcp` | 结束会话（`Mcp-Session-Id`） |
| GET | `/sse` | 旧版 SSE 通道，先下发 `endpoint` 事件 |
| POST | `/messages?sessionId=xxx` | 旧版 SSE 的客户端消息入口 |
| GET | `/healthz` | 健康检查（免鉴权，供监控探活） |
| GET | `/` | 服务信息 JSON |

鉴权：除 `/healthz` 外都需要 `Authorization: Bearer <MCP_AUTH_TOKEN>`；
SSE 客户端不便设请求头时可用 `?token=<MCP_AUTH_TOKEN>`。

手工验证（不依赖任何客户端）：

```bash
TOKEN=<TOKEN>
curl -s -X POST http://127.0.0.1:18088/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

---

## 五、配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FORTUNE_BASE_URL` | `http://fortune.example.com:46031/prod-api` | 记账系统接口根地址 |
| `FORTUNE_USERNAME` | `CHANGE_ME` | 登录账号 |
| `FORTUNE_PASSWORD` | `CHANGE_ME` | 明文密码（自动 RSA 加密） |
| `FORTUNE_ENCRYPTED_PASSWORD` | 空 | 抓包密文回放，无明文密码时用 |
| `FORTUNE_TOKEN` | 空 | 直接指定 JWT，跳过登录 |
| `FORTUNE_TOKEN_CACHE_DIR` | `~/.cache/fortune-mcp` | token 缓存目录（生产建议 `/var/lib/fortune-mcp`） |
| `FORTUNE_HTTP_TIMEOUT` | `20` | 下游请求超时（秒） |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `18088` | 监听地址与端口 |
| `MCP_PATH` | `/mcp` | Streamable HTTP 路径 |
| `MCP_AUTH_TOKEN` | 空 | **生产必填**，Bearer 令牌；为空时启动日志会告警 |
| `MCP_SESSION_TTL` | `3600` | 会话空闲回收秒数 |
| `FORTUNE_MULTI_TENANT` | `0` | 置 1 开启多用户隔离，按调用方身份取各自凭据 |
| `FORTUNE_USERS_FILE` | `/var/lib/fortune-mcp/users.json` | 凭据库路径 |
| `WB_ACCOUNT_ID` / `WB_CLIENT_ID` / `WB_CLIENT_SECRET` | 空 | 填齐即启用 access_token 校验（自建应用免授权），取值见第八节 |
| `WB_INTROSPECT` | 空 | 留空＝凭据齐全即启用；`1` 强制启用；`0` 强制关闭 |
| `WB_OAUTH_BASE` | `https://oauth2.account.tencent.com` | 身份服务根地址（私有化/异构域名时改） |
| `WB_FETCH_ALIAS` | `1` | 是否再调 userinfo 取 `alias_id`（工号）；`0` 则只用 `sub` |
| `WB_IDENTITY_CACHE_TTL` | `300` | 令牌校验结果缓存秒数（撤销令牌最长滞后这么久） |
| `WB_STRICT_AUD` | `0` | 置 1 校验令牌 `aud` 是否等于本应用 `client_id` |
| `WB_ALLOW_STATIC_TOKEN` | `1` | 是否仍接受 `MCP_AUTH_TOKEN`（运维/冒烟用，生产建议 `0`） |
| `MCP_USER_HEADERS` | `X-WorkBuddy-User,X-User-Id,…` | 身份头候选，仅在平台注入这类头时生效 |
| `FORTUNE_SINGLE_FALLBACK` | `0` | 认不出身份时是否退回单账号（默认不退回，避免记错账） |
| `FORTUNE_CRED_KEY` | 空 | 凭据加密密钥（hex/base64）；留空自动生成 `users.json.key` |
| `FORTUNE_ALLOW_HEADER_CRED` | `0` | 置 1 允许 `X-Fortune-Username/Password/Base-Url` 头覆盖账号（多用户共用一个连接器） |
| `MCP_TLS_CERT` / `MCP_TLS_KEY` | 空 | 服务自身启用 HTTPS（前面没有 nginx 时用） |
| `MCP_DEBUG` | `0` | 打印异常堆栈 |
| `MCP_DIAGNOSTIC` | `0` | 置 1 暴露 `fortune_diagnostic`，用于查看平台透传的请求头 |

---

## 六、多用户隔离（每人一个账套）

默认是**单账号**：服务端只持有一组 `.env` 凭据，所有人看到同一份数据。
要让每人只看到自己的账，开启多租户——**同一个连接器、同一份配置**，按调用方身份取各自的记账凭据。

**前提**：记账系统里每人有独立账号；服务端能认出「这是谁」（见下面第 0 步）。

### 0. 先确认身份从哪来

服务端认人的方式有两种，按连接器实际形态二选一：

| 形态 | 上游收到 | 服务端配置 |
|---|---|---|
| **自建应用免授权**（推荐） | `X-Oneid-Access-Token: <access_token>`，每人不同；部分版本用 `Authorization: Bearer` | 配齐 `WB_ACCOUNT_ID` / `WB_CLIENT_ID` / `WB_CLIENT_SECRET` 即自动启用 |
| 前置网关注入身份头 | `X-WorkBuddy-User` 等 | `FORTUNE_MULTI_TENANT=1` + `MCP_USER_HEADERS` |

> 实测记录：企业版该认证方式通过 `X-Oneid-Access-Token` 头传令牌，请求里**不会**出现 `Authorization`。
> 服务端同时接受 `Bearer`、裸 `Authorization`、`X-Oneid-Access-Token`、`X-MCP-Token`
> 与 `?token=` / `?access_token=`，因此不需要按平台版本改配置。

企业控制台的四种认证方式里，只有「自建应用免授权」会把**员工本人**的身份传下来；`API Key`、`OAuth 2.0` 都是连接器级固定凭据，全员同一身份，做不到按人隔离。

走第一种时，服务端把令牌交给平台校验：`introspect` 给出 `sub`（union_id），`userinfo` 按应用 Scope 给出 `alias_id` / `username` / `email`。服务端依次尝试 **alias_id → username → email → sub** 查凭据库，所以 `useradmin.py` 里 `--user` 填哪个都能命中。

> `userinfo` 返回哪些字段由自建应用的 Scope 决定：`profile` 提供 `username`，`email` 提供 `email`，`openid` 提供 `id_token`。三者都未授权时只能落到 `sub`。`--check-identity` 输出的 `aliasField` 会显示实际命中的字段。

可用一条命令自检：

```bash
python3 /opt/fortune-mcp/server.py --check-identity '<员工 access_token>'
# ok=true 且 boundUser 非空，说明身份链路和凭据映射都通了
```

### 1. 开启

```bash
# 方式一：交互式菜单（推荐，逐项提示 + 保存前确认 + 可立即验证登录）
python3 /opt/fortune-mcp/useradmin.py menu

# 方式二：脚本化（密码从标准输入读，不进 shell 历史）
echo '记账密码' | python3 /opt/fortune-mcp/useradmin.py add \
    --user demo_user --fortune-user demo_user --note '示例用户/研发部' --password-stdin

# 打开开关
sudo sed -i 's/^#*\s*FORTUNE_MULTI_TENANT=.*/FORTUNE_MULTI_TENANT=1/' /etc/fortune-mcp.env
sudo systemctl restart fortune-mcp
```

> 开关未开启时 `load_identity()` 直接返回空，凭据库不会加载——即使认出了员工身份，
> 也会退回 `.env` 里的单账号。启动日志中的 `多租户模式：凭据库 …（已绑定 N 人）`
> 是判断它是否生效的唯一依据，没有这一行就说明没生效。

### 2. 日常管理

```bash
python3 /opt/fortune-mcp/useradmin.py menu                 # 交互式菜单（增删改查都在里面）
python3 /opt/fortune-mcp/useradmin.py list                 # 查看全部映射
python3 /opt/fortune-mcp/useradmin.py disable --user demo_user   # 离职/停用
python3 /opt/fortune-mcp/useradmin.py remove  --user demo_user   # 删除
python3 /opt/fortune-mcp/useradmin.py template > users.csv      # CSV 模板
python3 /opt/fortune-mcp/useradmin.py import users.csv          # 批量导入
```

凭据库改动**不需要重启**（每次调用都重新读取）。

> CLI 启动时自动加载 `FORTUNE_ENV_FILE`、`/etc/fortune-mcp.env` 与 `.env`，
> 因此 `--verify` 校验账号时能直接读到 `FORTUNE_BASE_URL`；shell 中已 export 的变量优先。
> 需要指定其他文件时加 `--env-file <路径>`。

### 3. 行为约定

| 场景 | 服务端行为 |
|---|---|
| 身份头缺失 | 返回“未能识别当前用户身份”，**不会**落到别人账上 |
| 用户未绑定 | 返回可直接执行的 `useradmin.py add ...` 命令 |
| 用户已停用 | 返回“账号已被停用，请联系管理员” |
| 身份头命中多值 | 按 `MCP_USER_HEADERS` 顺序取第一个非空 |
| 传了别人的账户/分类/标签/账本 ID | 直接拒绝并列出本账号可用的 ID |

身份头候选（默认）：`X-WorkBuddy-User > X-User-Id > X-Employee-Id > X-Authenticated-User > Remote-User`，
可用 `MCP_USER_HEADERS` 改成平台实际使用的头名。

### 4. 安全

- 凭据库里**不存明文**：HMAC-SHA256 派生密钥流加密 + 加密后再 MAC，逐条 16 字节随机 nonce。
- 密钥来自 `FORTUNE_CRED_KEY`（推荐 KMS/Vault 注入），留空则自动生成 `users.json.key`（0600）。
- `users.json` 权限 0600，仅运行用户可读。
- 每次工具调用都写一行审计：`audit user=demo_user fortune=demo_user tool=fortune_add_expense ok`。
- 密钥丢失 = 凭据库不可解，只能重新导入；请把 `.key` 纳入备份。

### 5. 平台不透传身份怎么办

先跑第七节的诊断工具确认。若确实没有任何身份头，两条退路：
**一人一实例**（`install.sh --port` 多跑几次，后台建多个连接器）或
**本地 stdio 连接器**（每人自己机器配自己账号，见第八节）。

---

## 七、13 个工具

| 工具 | 类型 | 用途 |
|---|---|---|
| `fortune_context` | 只读 | 一次拿到分组、账本、账户与余额 |
| `fortune_list_accounts` | 只读 | 账户列表 + 余额合计 |
| `fortune_list_groups` / `fortune_list_books` | 只读 | 分组 / 账本 |
| `fortune_list_categories` / `fortune_list_payees` / `fortune_list_tags` | 只读 | 分类、收付款方、标签（按 `bill_type`） |
| `fortune_add_expense` | 写入 | 记支出（账户/分类可传 ID 或中文名） |
| `fortune_add_income` | 写入 | 记收入 |
| `fortune_add_transfer` | 写入 | 账户间转账 |
| `fortune_add_account` | 写入 | 新增账户 |
| `fortune_add_bill` | 写入 | 高级接口：多分类分摊、orderId |
| `fortune_login` | 写入 | 手动刷新令牌（一般无需调用） |

**诊断工具（默认关闭）**：设置 `MCP_DIAGNOSTIC=1` 后会多出第 14 个工具 `fortune_diagnostic`，返回本次调用的完整请求头快照、来源 IP、会话 ID 与当前生效账号（不含密码）。企业版连接器接入后调用它一次，就能确认平台是否透传了用户身份——这是判断能不能做“按人隔离账套”的关键依据。生产环境验证完请改回 `0`。

每个工具都带 `annotations`（`readOnlyHint` / `destructiveHint` / `idempotentHint`），
企业后台做只读授权、风险提示时可以直接用。

---

## 八、本地 stdio 调试（桌面客户端）

`~/.workbuddy/mcp.json`（Windows 用户目录为 `C:\Users\<用户名>\.workbuddy\mcp.json`）：

```json
{
  "mcpServers": {
    "fortune-bookkeeping": {
      "command": "python3",
      "args": ["/opt/fortune-mcp/server.py", "--stdio"],
      "env": {
        "FORTUNE_BASE_URL": "http://fortune.example.com:46031/prod-api",
        "FORTUNE_USERNAME": "CHANGE_ME",
        "FORTUNE_PASSWORD": "CHANGE_ME"
      }
    }
  }
}
```

改完需要重启客户端，再在「连接器 → 自定义连接器」里点“信任”。

---

## 九、排错

| 现象 | 原因 / 处理 |
|---|---|
| 报 `set: pipefail: invalid option name` 或 `invalid option nameset: pipefail` | 脚本被 Windows 换行污染（CRLF），`\r` 粘在 `pipefail` 后面 | `sed -i 's/\r$//' *.sh` 后重跑；新版脚本已内置自愈，会自动转 LF 再执行 |
| `无法连接记账服务 …` | 服务器到 `fortune.example.com:46031` 不通：`curl -v http://fortune.example.com:46031/prod-api/getRsaPublicKey`；检查路由/防火墙 |
| `HTTP 401` 调 MCP 接口 | 服务端日志会打印 `401 … mode=…` 与请求头清单，按下面的「401 排查流程」判断 |
| 启动失败：`缺少模块 'user_store'` | 部署目录没拷全，补 `user_store.py`、`useradmin.py` 后重启 |
| 启动失败：`密钥文件不可读` 或 `Permission denied` | 数据目录属主不对：`chown -R fortune:fortune /var/lib/fortune-mcp` |
| 多租户不生效（所有人同一份账） | 启动日志缺少 `多租户模式：凭据库 …`，检查 `FORTUNE_MULTI_TENANT=1` |
| `login 失败：缺少密码` | `.env` 未加载；systemd 用 `EnvironmentFile`，手工启动需 `set -a; . .env; set +a` |
| `接口错误 code=500 msg=…` | 下游业务校验失败，原文会透传在 `content.text` 里 |
| Agent 不调用工具 | 工具描述被裁剪或名称冲突：确认 `tools/list` 返回 13 个工具，且后台勾选了这些工具 |
| SSE 一直不返回 | 中间代理开了缓冲：确保 `proxy_buffering off`（nginx 配置已含） |
| 会话频繁失效 | 负载均衡多实例但未做会话粘滞；单实例部署或开启粘滞（`Mcp-Session-Id`） |

日志：`journalctl -u fortune-mcp -f`（用户级：`journalctl --user -u fortune-mcp -f`）。

### 401 排查流程

每次鉴权失败都会打印三行诊断，第一步永远是看这三行：

```
401 POST /mcp | Authorization=none | X-MCP-Token=no | query-keys=- | cookie=no | mode=introspect
    headers: accept,content-length,content-type,host,x-oneid-access-token
    user-agent: WorkBuddy/5.11.3
```

按 `headers` 行判断令牌有没有到达：

| 观察 | 结论 | 处理 |
|---|---|---|
| 出现 `x-oneid-access-token` | 令牌已到达 | 问题在校验环节，看返回消息里的 `invalid_client` / `令牌无效` |
| 只有标准头，无任何 token 字样 | 平台没有换出令牌 | 连接器认证方式选「自建应用免授权」，保存后重新发布 |
| 出现 `authorization` | 走静态令牌或 Bearer | 核对 `MCP_AUTH_TOKEN` 与连接器 Header 值是否一致 |

再看 `mode`：

- `mode=introspect`：access_token 校验已启用，令牌会被交给平台校验
- `mode=static`：`WB_*` 没读到，检查配置文件权限与三项凭据是否填齐

返回消息也能辅助判断：

| 消息 | 含义 |
|---|---|
| 只有「缺少或错误的 Bearer Token」 | 令牌没到服务端，`_identity_error` 为空 |
| 「缺少或错误的 Bearer Token（introspect 返回 HTTP 401…）」 | 令牌到了，但 `client_id` / `client_secret` 不对 |
| 「缺少或错误的 Bearer Token（令牌未通过 introspect 校验…）」 | 令牌已过期、被撤销，或不属于本企业 |

命令行复核：

```bash
sudo -u fortune python3 /opt/fortune-mcp/server.py --check-identity '<access_token>'
```

`ok=true` 后看 `aliasField`（实际命中的字段）与 `boundUser`（绑定的凭据库键）；`boundUser` 为空说明身份通了但还没绑账套。

---

## 十、安全建议

1. **必须设置 `MCP_AUTH_TOKEN`**，长度 ≥ 32 位随机串；令牌泄露等于记账权限泄露。
2. 不要把 `46031` 端口或本服务直接暴露到公网；前置 nginx + HTTPS + 出口 IP 白名单。
3. `.env` / `/etc/fortune-mcp.env` 权限 600，仅运行用户可读。
4. 生产建议关掉 `MCP_DEBUG`，日志只留调用摘要。
5. **所有 ID 都做归属校验**：账户 / 分类 / 标签 / 收付款方 / 账本在写入前都会核对该用户自己的数据，
   不匹配即拒绝。原因很实在——实测发现下游服务端**不校验账户是否属于该账本**，
   若代理层不拦，任何人填一个别人的账户 ID 就能扣到对方账上（已修复并加回归测试）。
6. 下游业务系统自身的权限校验不要绕过——本服务只是代理层。
7. 定期轮换令牌：改 `MCP_AUTH_TOKEN` → `systemctl restart fortune-mcp` → 同步更新连接器配置。
