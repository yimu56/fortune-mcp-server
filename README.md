# Fortune 记账 MCP Server

把 Fortune 记账系统（RuoYi 风格 `prod-api`）封装成**可远程部署的 MCP 服务**，供 WorkBuddy 企业版连接器调用。
纯 Python 标准库实现，**零第三方依赖**，单机常驻、内存占用 < 60 MB。

- 传输协议：**Streamable HTTP**（推荐，`POST /mcp`）+ **旧版 HTTP+SSE**（兼容）
- 同时支持 **stdio** 模式（本地 `mcp.json` 调试）
- 工具数：**13 个**（查询账户/账本/分类/标签/收付款方 + 记账/转账/建账户）
- 默认下游地址：`http://fortune.example.com:46031/prod-api`（占位示例，部署时须按实际环境覆盖）
- 当前版本：**v2.0.0**（首个公开发布版本）

---

## 一、目录结构

```
fortune-mcp-server/
├── server.py                     # 服务入口：HTTP + SSE + stdio
├── fortune_api.py                # 记账系统 HTTP 客户端（含手写 RSA 加密登录）
├── fortune_tools.py              # 13 个 MCP 工具定义与实现
├── smoke_test.py                 # 冒烟测试（可在服务器上直接跑）
├── healthcheck.py                # 容器健康检查
├── install.sh                    # 一键安装（systemd / 用户级服务）
├── uninstall.sh                  # 卸载（支持保留配置/彻底清除/dry-run）
├── .env.example                  # 配置样例
├── Dockerfile / docker-compose.yml
├── deploy/
│   ├── fortune-mcp.service       # systemd 单元参考
│   └── nginx-fortune-mcp.conf    # HTTPS 反代参考（SSE 安全配置）
├── README.md                     # 本文
├── CHANGELOG.md                  # 版本变更记录
├── LICENSE                       # MIT 许可（Copyright (c) 2026 yimu56）
├── .gitignore                    # 忽略本地配置与缓存
└── docs/enterprise-connector-publish-guide.md    # 后台发布步骤 + 验收清单
```

---

## 二、5 分钟部署（Linux）

### 1. 上传并安装

在**能访问记账系统的 Linux 服务器**上执行：

```bash
# 方式一：scp 上传整个目录
scp -r fortune-mcp-server user@<服务器地址>:/tmp/
ssh user@<服务器地址>
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
sudo cp server.py fortune_api.py fortune_tools.py /opt/fortune-mcp/
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
  · fortune_add_transfer  ok  余额已复原 (100.00 / 50.00)
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
| `FORTUNE_ALLOW_HEADER_CRED` | `0` | 置 1 允许 `X-Fortune-Username/Password/Base-Url` 头覆盖账号（多用户共用一个连接器） |
| `MCP_TLS_CERT` / `MCP_TLS_KEY` | 空 | 服务自身启用 HTTPS（前面没有 nginx 时用） |
| `MCP_DEBUG` | `0` | 打印异常堆栈 |

---

## 六、13 个工具

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

每个工具都带 `annotations`（`readOnlyHint` / `destructiveHint` / `idempotentHint`），
企业后台做只读授权、风险提示时可以直接用。

---

## 七、本地 stdio 调试（桌面客户端）

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

## 八、排错

| 现象 | 原因 / 处理 |
|---|---|
| 报 `set: pipefail: invalid option name` 或 `invalid option nameset: pipefail` | 脚本被 Windows 换行污染（CRLF），`\r` 粘在 `pipefail` 后面 | `sed -i 's/\r$//' *.sh` 后重跑；新版脚本已内置自愈，会自动转 LF 再执行 |
| `无法连接记账服务 …` | 服务器到 `fortune.example.com:46031` 不通：`curl -v http://fortune.example.com:46031/prod-api/getRsaPublicKey`；检查路由/防火墙 |
| `HTTP 401` 调 MCP 接口 | 没带 `Authorization: Bearer`，或令牌与 `MCP_AUTH_TOKEN` 不一致 |
| `连接器保存时报连接超时` | 平台侧到服务器网络不通（内网地址！见教程第 1 节） |
| `login 失败：缺少密码` | `.env` 未加载；systemd 用 `EnvironmentFile`，手工启动需 `set -a; . .env; set +a` |
| `接口错误 code=500 msg=…` | 下游业务校验失败，原文会透传在 `content.text` 里 |
| Agent 不调用工具 | 工具描述被裁剪或名称冲突：确认 `tools/list` 返回 13 个工具，且后台勾选了这些工具 |
| SSE 一直不返回 | 中间代理开了缓冲：确保 `proxy_buffering off`（nginx 配置已含） |
| 会话频繁失效 | 负载均衡多实例但未做会话粘滞；单实例部署或开启粘滞（`Mcp-Session-Id`） |

日志：`journalctl -u fortune-mcp -f`（用户级：`journalctl --user -u fortune-mcp -f`）。

---

## 九、安全建议

1. **必须设置 `MCP_AUTH_TOKEN`**，长度 ≥ 32 位随机串；令牌泄露等于记账权限泄露。
2. 不要把 `46031` 端口或本服务直接暴露到公网；前置 nginx + HTTPS + 出口 IP 白名单。
3. `.env` / `/etc/fortune-mcp.env` 权限 600，仅运行用户可读。
4. 生产建议关掉 `MCP_DEBUG`，日志只留调用摘要。
5. 下游业务系统自身的权限校验不要绕过——本服务只是代理层。
6. 定期轮换令牌：改 `MCP_AUTH_TOKEN` → `systemctl restart fortune-mcp` → 同步更新连接器配置。

---

## 十、版本与许可

- 当前版本：**v2.0.0**，与 `SERVER_VERSION`、Docker 镜像标签及发布标签一致。
- 变更记录：见 [CHANGELOG.md](CHANGELOG.md)。
- 许可：MIT，详见 [LICENSE](LICENSE)，版权署名 `yimu56`。
- 发布提醒：提交前确认 `.env`、`*.env.bak` 等本地配置未被纳入版本库（见 [.gitignore](.gitignore)）。
