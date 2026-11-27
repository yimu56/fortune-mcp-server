# 更新日志

版本号遵循语义化版本（SemVer），与 `fortune_tools.py` 中的 `SERVER_VERSION`、
镜像标签及发布标签保持一致。

## 未发布

修复 root 安装时忽略 `.env` 的问题，以及四处影响正确性与暴露面的缺陷。

### 修复

- `user_store.py`：凭据库支持热加载。每次取凭据前核对 `users.json` 的修改时间、大小与 inode，
  被其他进程（如 `useradmin.py`）改写后自动重新加载，与文档中「改动不需要重启」的约定一致；
  此前新增、停用、删除的用户都要重启服务才生效。运行期重新加载失败时沿用原有数据并输出告警。
- `fortune_tools.py`：账户名、分类名模糊匹配命中多个时拒绝调用并列出候选，
  不再静默选中第一个；此前同前缀的账户（如「现金」与「备用现金」）可能记错账户。
- `fortune_tools.py`：金额校验拦截 `nan` / `inf` 等非有限数字，此前会被放行并提交给下游。
- `server.py`：免鉴权的 `/healthz` 不再返回记账登录名，只给出账号是否已配置；
  命令行 `--self-check` 仍展示登录名。

### 新增

- 离线回归测试 `tests/test_regressions.py`（标准库 `unittest`，不依赖下游记账系统）：
  `python3 -m unittest discover -s tests`

### 安装脚本

- `install.sh`：新增 `--env-file`，并按 `--env-file` > `FORTUNE_ENV_FILE` > 脚本目录 `.env` > 当前目录 `.env`
  的顺序加载配置；解析规则与 `server.py` 的 `load_env_files` 一致，已存在的环境变量保持优先。
- 首次安装时若找到 `.env`，原样安装为目标配置（保留全部配置项与注释、规范 CRLF），再按需修正
  `FORTUNE_TOKEN_CACHE_DIR` 与 `MCP_PORT`；`MCP_AUTH_TOKEN` 仍为样例占位符时自动生成随机令牌。
- 目标配置已存在时保留原值，并提示检测到未生效的 `.env`。

## v3.1.3 — 2026-11-26

修复 root 运行 `useradmin.py` 后服务无法启动的问题。

### 修复

- `user_store.py`：root 身份写入 `users.json` / 密钥文件后，自动把属主纠正为
  数据目录（如 `/var/lib/fortune-mcp`）的所有者。此前 `sudo python3 useradmin.py add`
  会把 0600 的凭据库写成 `root:root`，而服务以 `fortune` 用户运行，重启即因
  `PermissionError` 崩溃进入 auto-restart 循环。

## v3.1.2 — 2026-11-26

修复「自建应用免授权」下平台通过自定义头传递令牌导致全程 401 的问题，补齐命令行自检的配置读取。

### 修复

- 鉴权载体新增 `X-Oneid-Access-Token` 等自定义令牌头：WorkBuddy 企业版该认证方式
  通过这类头传 access_token，不使用 `Authorization`，此前所有请求都被判为「无令牌」。
  查询参数同时接受 `token` 与 `access_token`。
- `_deny()` 输出请求头清单与 User-Agent；401 响应改为 `Connection: close` 并消费请求体，
  消除残留字节引发的 `Bad request syntax` 400。
- `user_store.py` 读写密钥文件失败时给出可执行的权限修复命令，
  替代原先直接抛出的 `PermissionError`。
- 诊断工具的请求头快照对 `X-Oneid-Access-Token`、`Cookie`、`X-Fortune-Password`
  等凭据类头一并打码，避免排障接口成为令牌泄露通道。

### 新增

- `server.py` 命令行模式（`--check-identity` / `--self-check`）自动加载环境变量文件，
  新增 `--env-file`；文件不可读时输出明确警告，不再误报「未启用 access_token 校验」。

### 文档

- README 补充认证载体清单、部署完整性要求与 401 排查路径。
- 版本升至 3.1.2，`SERVER_VERSION` 与镜像标签同步。

## v3.1.1 — 2026-11-26

修复连接器走 SSE 通道时 401 难以定位，以及身份字段取不到导致绑定键退化为 union_id 的问题。

### 修复

- `_bearer()` 兼容不带 `Bearer ` 前缀的 `Authorization`（部分平台直接放令牌），
  同时继续接受 `X-MCP-Token` 与 `?token=`；`Basic` / `Digest` 等前缀仍拒绝。
- `_deny()` 输出 401 诊断日志：请求方法、路径、Authorization 形态（none / bearer / raw）、
  其他令牌载体与当前鉴权模式，可区分「未带令牌」与「令牌校验失败」。
- 身份字段取值扩展为 `alias_id` → `username` → `email` → `sub`，
  修复应用 Scope 未配置别名时只能绑定 union_id 的问题；新增 `aliasField`
  标明实际命中字段，诊断工具与 `--check-identity` 同步展示。

### 文档

- README 补充 Scope 与 `userinfo` 返回字段的对应关系，以及候选键顺序。
- 版本升至 3.1.1，`SERVER_VERSION` 与镜像标签同步。

## v3.1.0 — 2026-11-26

修复企业版连接器「自建应用免授权」下无法识别用户、所有调用报
「未能识别当前用户身份」的问题。

### 修复

- 新增 access_token 身份校验（`WbIdentity`）：按官方文档调用
  `introspect` 验令牌、`userinfo` 取 `alias_id`，把员工身份映射到凭据库。
  该认证方式下平台只传 `Authorization: Bearer <access_token>`，
  **不会**注入 `X-User-Id` / `X-WorkBuddy-User` 等身份头，旧的头部识别逻辑
  在这种形态下必然失败。
- `_authorized()` 改为三路：服务端静态令牌 → 员工 access_token → 未配置鉴权。
  校验结果按令牌哈希缓存（默认 300s），单次请求只往返一次身份服务。
- 凭据库查找改为候选键：先 `alias_id`、后 `sub`，命中即返回；未绑定时
  直接给出可执行的 `useradmin.py add` 命令。
- 校验企业归属：令牌 `tid` 与服务端 `WB_ACCOUNT_ID` 不一致时拒绝；
  可选 `WB_STRICT_AUD=1` 再校验 `aud` 是否含本应用 `client_id`。

### 新增

- `server.py --check-identity <token>`：一条命令验证令牌能否解析出身份
  并命中凭据库，不用等连接器回调。
- 诊断工具 `fortune_diagnostic` 增加 `workbuddyIdentity` 段
  （mode / sub / aliasId / accountId / jti / candidates）。
- 环境变量：`WB_ACCOUNT_ID`、`WB_CLIENT_ID`、`WB_CLIENT_SECRET`、
  `WB_INTROSPECT`、`WB_OAUTH_BASE`、`WB_FETCH_ALIAS`、
  `WB_IDENTITY_CACHE_TTL`、`WB_STRICT_AUD`、`WB_ALLOW_STATIC_TOKEN`。
- 版本升至 3.1.0，`SERVER_VERSION` 与镜像标签同步。

## v3.0.0 — 2026-11-26

在 v2.0.0 基础上恢复并强化多租户与写入安全能力，作为新的公开发布版本。

### 功能

- 新增多租户「一人一账套」：加密凭据库 `user_store.py`、管理 CLI `useradmin.py`、
  批量加人脚本 `deploy/add-tenant.sh`，按身份请求头映射各人独立的记账账号。
- 新增诊断工具 `fortune_diagnostic`（`MCP_DIAGNOSTIC=1` 开启）：返回请求头快照，
  用于确认平台是否透传用户身份。
- 新增写入归属校验：账本、账户、分类、标签、收付款方在写入前核对归属，
  拒绝跨账套引用与相同账户互转，修复下游服务端不校验账户归属的问题。
- 新增逐次调用的审计日志，记录平台用户、记账账号、工具名与执行结果。

### 变更

- 版本升级至 3.0.0，`SERVER_VERSION` 与 Docker 镜像标签同步更新。
- `useradmin.py` 启动时自动加载 `FORTUNE_ENV_FILE`、`/etc/fortune-mcp.env` 与 `.env`，
  修复手工执行校验时读不到 `FORTUNE_BASE_URL`、回落到占位地址的问题；新增 `--env-file` 参数。
- 服务启动失败时输出可读原因（缺少模块 / 端口占用 / 文件权限），
  并修正手工部署步骤遗漏 `user_store.py`、`useradmin.py` 的问题。
- 恢复 MIT LICENSE、CHANGELOG 与 .gitignore；实施手册改为英文路径，便于跨平台协作。
- 发布前完成全量脱敏：内网地址、测试账号、口令与姓名示例统一替换为占位符。

## v2.0.0 — 2026-11-25

首个公开发布版本。

### 功能

- 基于 Streamable HTTP（`POST /mcp`）的 MCP 服务，兼容旧版 HTTP+SSE 与本地 stdio 模式。
- 提供 13 个记账工具：分组/账本/账户/分类/收付款方/标签查询，以及支出、收入、转账、账户与高级记账写入。
- 纯 Python 标准库实现，零第三方依赖；纯手写 RSA PKCS#1 v1.5 加密登录，JWT 令牌本地缓存复用。
- 支持 Bearer Token 鉴权、会话 TTL 回收、可选 TLS、多用户凭据头覆盖。
- 提供 systemd、用户级服务、Docker Compose 三种常驻部署方式与配套安装/卸载脚本。

### 变更

- 发布前完成全量脱敏：示例中的账号口令、内网地址、余额金额统一替换为占位符。
- 安装脚本在生成配置时提示修改占位账号口令，避免直接使用默认值登录。
- 企业版连接器发布教程重命名为英文路径，便于跨平台协作。

### 文档

- README 覆盖部署、验证、卸载、端点、配置项、工具清单、本地调试、排错与安全建议。
- 新增 CHANGELOG、MIT LICENSE 与 .gitignore。
