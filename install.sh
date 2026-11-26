#!/usr/bin/env bash
# 若文件被 Windows 换行污染（CRLF），先自愈再执行，否则会报 "invalid option name"（本行以注释结尾，CRLF 不影响解析）
grep -qU $'\r' "$0" 2>/dev/null && sed -i 's/\r$//' "$0" && exec bash "$0" "$@"; : # crlf-guard

# ---------------------------------------------------------------------------
# Fortune 记账 MCP Server 一键安装脚本
#
#   sudo bash install.sh                   # root：装到 /opt/fortune-mcp + systemd 系统服务
#   bash install.sh                        # 普通用户：装到 ~/fortune-mcp + systemd --user
#   sudo bash install.sh --port 18088 --dir /opt/fortune-mcp
#   sudo bash install.sh --no-firewall        # 不改防火墙
#   sudo bash install.sh --uninstall          # 停止并卸载（保留配置与缓存）
#
# 安装完成后会打印：服务地址、Bearer Token、自检命令。
# ---------------------------------------------------------------------------
set -euo pipefail

SERVICE="fortune-mcp"
PORT="${MCP_PORT:-18088}"
APP_DIR="${APP_DIR:-}"
DO_FIREWALL=1
UNINSTALL=0
UN_EXTRA=()
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --dir) APP_DIR="$2"; shift 2 ;;
        --no-firewall) DO_FIREWALL=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --yes|-y|--purge|--dry-run|--keep-app|--close-firewall)
            UN_EXTRA+=("$1"); shift ;;
        -h|--help) awk '/^# -{10,}/{n++; if(n==2){print; exit}} n>=1 {print}' "$0"; exit 0 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

info()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1
if [ -z "$APP_DIR" ]; then
    if [ $IS_ROOT -eq 1 ]; then APP_DIR="/opt/fortune-mcp"; else APP_DIR="$HOME/fortune-mcp"; fi
fi

# ---------------------------------------------------------------- 卸载
if [ $UNINSTALL -eq 1 ]; then
    # 卸载逻辑统一交给 uninstall.sh，避免两份实现走偏
    [ -f "$SRC_DIR/uninstall.sh" ] || die "缺少 $SRC_DIR/uninstall.sh，无法卸载"
    UN_ARGS=(--port "$PORT" --dir "$APP_DIR")
    exec bash "$SRC_DIR/uninstall.sh" "${UN_ARGS[@]}" ${UN_EXTRA[@]+"${UN_EXTRA[@]}"}
fi

# ---------------------------------------------------------------- 依赖检查
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装（Python 3.8+）"
PY="$(command -v python3)"
"$PY" - <<'EOF' || die "Python 版本过低，需要 3.8+"
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
EOF
info "Python: $($PY -V 2>&1)"

for f in server.py fortune_api.py fortune_tools.py user_store.py useradmin.py uninstall.sh; do
    [ -f "$SRC_DIR/$f" ] || die "缺少文件 $SRC_DIR/$f（请在解压后的目录内执行本脚本）"
done

gen_token() { "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))'; }

# ---------------------------------------------------------------- 复制程序
mkdir -p "$APP_DIR"
cp -f "$SRC_DIR/server.py" "$SRC_DIR/fortune_api.py" "$SRC_DIR/fortune_tools.py" \
      "$SRC_DIR/smoke_test.py" "$SRC_DIR/user_store.py" "$SRC_DIR/useradmin.py" \
      "$SRC_DIR/uninstall.sh" "$APP_DIR/"
chmod +x "$APP_DIR/uninstall.sh" 2>/dev/null || true
if [ -f "$SRC_DIR/deploy/add-tenant.sh" ]; then
    cp -f "$SRC_DIR/deploy/add-tenant.sh" "$APP_DIR/"; chmod +x "$APP_DIR/add-tenant.sh" 2>/dev/null || true
fi
info "程序已复制到 $APP_DIR"

if [ $IS_ROOT -eq 1 ]; then
    CACHE_DIR="/var/lib/fortune-mcp"
    ENV_FILE="/etc/fortune-mcp.env"
    id -u fortune >/dev/null 2>&1 || useradd -r -s /sbin/nologin -d "$CACHE_DIR" fortune \
        || warn "创建 fortune 用户失败，将用 nobody 运行"
    RUN_USER=fortune
    id -u fortune >/dev/null 2>&1 || RUN_USER=nobody
else
    CACHE_DIR="$APP_DIR/data"
    ENV_FILE="$APP_DIR/.env"
    RUN_USER="$(id -un)"
fi
mkdir -p "$CACHE_DIR"
[ $IS_ROOT -eq 1 ] && chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR" "$CACHE_DIR" 2>/dev/null || true

# ---------------------------------------------------------------- 配置文件
if [ -f "$ENV_FILE" ]; then
    info "已存在配置 $ENV_FILE，保留原值（如需改端口/令牌请手动编辑）"
    TOKEN="$(grep -E '^MCP_AUTH_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    PORT="$(grep -E '^MCP_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2- || echo "$PORT")"
else
    TOKEN="${MCP_AUTH_TOKEN:-$(gen_token)}"
    umask 077
    cat > "$ENV_FILE" <<EOF
# Fortune 记账 MCP Server 配置（安装脚本生成）
FORTUNE_BASE_URL=${FORTUNE_BASE_URL:-http://fortune.example.com:46031/prod-api}
FORTUNE_USERNAME=${FORTUNE_USERNAME:-CHANGE_ME}
FORTUNE_PASSWORD=${FORTUNE_PASSWORD:-CHANGE_ME}
FORTUNE_TOKEN_CACHE_DIR=${CACHE_DIR}
FORTUNE_HTTP_TIMEOUT=20

MCP_HOST=0.0.0.0
MCP_PORT=${PORT}
MCP_PATH=/mcp
MCP_AUTH_TOKEN=${TOKEN}
MCP_SESSION_TTL=3600
MCP_DEBUG=0
MCP_DIAGNOSTIC=0
FORTUNE_ALLOW_HEADER_CRED=0

# ===== 多用户隔离（每人一个记账账号）=====
# 开启后按请求头识别用户，再到凭据库里取各自的记账账号
# FORTUNE_MULTI_TENANT=1
# FORTUNE_USERS_FILE=/var/lib/fortune-mcp/users.json
# MCP_USER_HEADERS=X-WorkBuddy-User,X-User-Id,X-Employee-Id,X-Authenticated-User
# FORTUNE_SINGLE_FALLBACK=0
EOF
    umask 022
    [ $IS_ROOT -eq 1 ] && chown "$RUN_USER":"$RUN_USER" "$ENV_FILE" && chmod 600 "$ENV_FILE"
    info "已生成配置 $ENV_FILE（令牌为随机生成，见文末）"
fi

# ---------------------------------------------------------------- systemd
if [ $IS_ROOT -eq 1 ]; then
    cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Fortune Bookkeeping MCP Server
Documentation=https://www.workbuddy.cn/docs/workbuddy/Overview
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=HOME=${CACHE_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PY} ${APP_DIR}/server.py
Restart=always
RestartSec=3
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${CACHE_DIR}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE}

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$SERVICE"
    info "系统服务已启动：systemctl status $SERVICE"
    LOG_HINT="journalctl -u $SERVICE -f"
elif systemctl --user list-unit-files >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/${SERVICE}.service" <<EOF
[Unit]
Description=Fortune Bookkeeping MCP Server (user)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PY} ${APP_DIR}/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE"
    loginctl enable-linger "$(id -un)" 2>/dev/null || warn "无法设置 linger，注销后服务可能停止"
    info "用户级服务已启动：systemctl --user status $SERVICE"
    LOG_HINT="journalctl --user -u $SERVICE -f"
else
    cat > "$APP_DIR/run.sh" <<EOF
#!/usr/bin/env bash
export \$(grep -v '^#' ${ENV_FILE} | xargs)
exec ${PY} ${APP_DIR}/server.py
EOF
    chmod +x "$APP_DIR/run.sh"
    warn "当前环境无 systemd，请手动常驻运行：bash $APP_DIR/run.sh"
    LOG_HINT="前台输出"
fi

# ---------------------------------------------------------------- 防火墙
if [ $DO_FIREWALL -eq 1 ] && [ $IS_ROOT -eq 1 ]; then
    if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null && firewall-cmd --reload >/dev/null
        info "firewalld 已放行 ${PORT}/tcp"
    elif command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
        ufw allow "${PORT}/tcp" >/dev/null
        info "ufw 已放行 ${PORT}/tcp"
    else
        warn "未检测到 firewalld/ufw 活动规则，请自行确认 ${PORT}/tcp 是否放行"
    fi
fi

# ---------------------------------------------------------------- 健康检查
info "等待服务就绪…"
OK=0
for _ in $(seq 1 20); do
    if "$PY" - "$PORT" <<'EOF' 2>/dev/null
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen("http://127.0.0.1:%s/healthz" % port, timeout=2) as r:
    d = json.loads(r.read())
sys.exit(0 if d.get("status") == "ok" else 1)
EOF
    then OK=1; break; fi
    sleep 1
done

LAN_IP="$("$PY" -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); print(s.getsockname()[0])
except Exception:
    print("127.0.0.1")
finally:
    s.close()' 2>/dev/null || echo 127.0.0.1)"

echo
if [ $OK -eq 1 ]; then
    info "服务已就绪 ✔"
else
    warn "健康检查未通过，请查看日志：$LOG_HINT"
fi
cat <<EOF

────────────────────────────────────────────────────────
 企业版连接器需要填写的信息
────────────────────────────────────────────────────────
 连接方式      Streamable HTTP（MCP）
 服务地址      http://${LAN_IP}:${PORT}/mcp
 兼容 SSE 地址 http://${LAN_IP}:${PORT}/sse
 鉴权方式      Bearer Token
 令牌          ${TOKEN}

 多用户隔离（可选）
   开启：编辑 ${ENV_FILE}，取消 FORTUNE_MULTI_TENANT 等注释后重启
   加人：python3 ${APP_DIR}/useradmin.py menu      # 交互式菜单，跟着提示填（推荐）
         或：echo '密码' | python3 ${APP_DIR}/useradmin.py add --user demo_user --fortune-user demo_user --password-stdin
   查看：python3 ${APP_DIR}/useradmin.py list

 本机自检
   python3 ${APP_DIR}/smoke_test.py http://127.0.0.1:${PORT}/mcp '${TOKEN}' --write
 登录/连通性自检
   set -a; . ${ENV_FILE}; set +a; python3 ${APP_DIR}/server.py --self-check
 日志
   ${LOG_HINT}
────────────────────────────────────────────────────────
EOF
