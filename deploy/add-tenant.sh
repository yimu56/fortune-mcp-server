#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 为「一人一实例」模式批量追加租户实例（模式 B）
#
#   sudo bash add-tenant.sh <英文名> <记账账号> <密码> [端口]
#   例：sudo bash add-tenant.sh demo_user 示例用户 'CHANGE_ME' 18091
#
# 前置：已用 install.sh 完成首个实例（/opt/fortune-mcp + fortune-mcp 服务）
# 产物：/opt/fortune-mcp-<名>、/etc/fortune-mcp-<名>.env、
#       fortune-mcp-<名>.service、监听 <端口>
#
# 注意：这是「平台不透传用户身份」时的退路。能透传就用多租户模式（更省事）。
# ---------------------------------------------------------------------------
set -euo pipefail

if [ $# -lt 3 ]; then
    sed -n '2,14p' "$0"
    exit 1
fi

NAME="$1"
FUSER="$2"
FPASS="$3"
PORT="${4:-18091}"
DIR="/opt/fortune-mcp-${NAME}"
DATA="/var/lib/fortune-mcp-${NAME}"
ENVF="/etc/fortune-mcp-${NAME}.env"
UNIT="/etc/systemd/system/fortune-mcp-${NAME}.service"
BASE_DIR="/opt/fortune-mcp"
BASE_UNIT="/etc/systemd/system/fortune-mcp.service"
BASE_ENV="/etc/fortune-mcp.env"

info() { printf '\033[32m[+]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行：sudo bash $0 ..."
[ -f "$BASE_UNIT" ] || die "找不到 $BASE_UNIT，请先跑 install.sh 完成首个实例"
[ -f "$BASE_ENV" ]  || die "找不到 $BASE_ENV"
[ -d "$BASE_DIR" ]  || die "找不到 $BASE_DIR"
[ -e "$DIR" ] && info "目录已存在，将覆盖程序文件（配置与数据保留）"

if ss -lptn "sport = :${PORT}" 2>/dev/null | grep -q LISTEN; then
    die "端口 ${PORT} 已被占用，换一个：ss -lptn 'sport = :${PORT}'"
fi
if id -u "fortune-${NAME}" >/dev/null 2>&1; then
    info "运行用户 fortune-${NAME} 已存在，复用"
else
    useradd -r -s /sbin/nologin -d "$DATA" "fortune-${NAME}"
    info "已创建运行用户 fortune-${NAME}"
fi

mkdir -p "$DIR" "$DATA"
cp -f "$BASE_DIR"/{server.py,fortune_api.py,fortune_tools.py,user_store.py,useradmin.py,smoke_test.py} "$DIR/"

# 从首个实例派生独立的 unit（服务名/目录/端口/用户全部替换）
sed -e "s#${BASE_DIR}#${DIR}#g" \
    -e "s#fortune-mcp#fortune-mcp-${NAME}#g" \
    -e "s#${BASE_ENV}#${ENVF}#g" \
    -e "s#/var/lib/fortune-mcp#${DATA}#g" \
    -e "s#^User=fortune#User=fortune-${NAME}#" \
    -e "s#^Group=fortune#Group=fortune-${NAME}#" \
    -e "s#18088#${PORT}#g" \
    "$BASE_UNIT" > "/tmp/fm-${NAME}.service"

# 派生独立 env（换账号、换端口、换缓存目录）
sed -e "s#/var/lib/fortune-mcp#${DATA}#g" \
    -e "s#^FORTUNE_USERNAME=.*#FORTUNE_USERNAME=${FUSER}#" \
    -e "s#^FORTUNE_PASSWORD=.*#FORTUNE_PASSWORD=${FPASS}#" \
    -e "s#^MCP_PORT=.*#MCP_PORT=${PORT}#" \
    "$BASE_ENV" > "/tmp/fm-${NAME}.env"

mv "/tmp/fm-${NAME}.service" "$UNIT"
mv "/tmp/fm-${NAME}.env" "$ENVF"
chown -R "fortune-${NAME}:fortune-${NAME}" "$DIR" "$DATA"
chown "fortune-${NAME}:fortune-${NAME}" "$ENVF"
chmod 600 "$ENVF"

systemctl daemon-reload
systemctl enable --now "fortune-mcp-${NAME}" >/dev/null 2>&1 || true

sleep 2
if curl -s -m 5 "http://127.0.0.1:${PORT}/healthz" | grep -q '"status": "ok"'; then
    info "实例就绪：http://<本机IP>:${PORT}/mcp   （账号 ${FUSER}）"
else
    printf '\033[33m[!]\033[0m 健康检查未通过，看日志：journalctl -u fortune-mcp-%s -n 50 --no-pager\n' "$NAME"
fi

cat <<EOF

────────────────────────────────────────────────────────
 下一个连接器填写信息（后台建「记账-${NAME}」并只授权给本人）
────────────────────────────────────────────────────────
 服务地址  http://<本机IP>:${PORT}/mcp
 鉴权      Bearer <取 ${ENVF} 里的 MCP_AUTH_TOKEN>
 账本账号  ${FUSER}
────────────────────────────────────────────────────────
EOF
