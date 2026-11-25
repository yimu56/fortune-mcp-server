#!/usr/bin/env bash
# 若文件被 Windows 换行污染（CRLF），先自愈再执行，否则会报 "invalid option name"（本行以注释结尾，CRLF 不影响解析）
grep -qU $'\r' "$0" 2>/dev/null && sed -i 's/\r$//' "$0" && exec bash "$0" "$@"; : # crlf-guard

# ---------------------------------------------------------------------------
# Fortune 记账 MCP Server 卸载脚本
#
#   sudo bash uninstall.sh                 # 交互确认后卸载（保留配置与缓存）
#   sudo bash uninstall.sh --yes           # 不确认，直接卸载（保留配置与缓存）
#   sudo bash uninstall.sh --yes --purge   # 连配置、缓存、运行用户、防火墙端口一起清掉
#   sudo bash uninstall.sh --dry-run       # 只打印将要执行的动作，不动任何东西
#
# 默认行为（保守）：
#   ✅ 停止并禁用服务     ✅ 删除 systemd 单元     ✅ 结束残留进程
#   ✅ 删除程序目录
#   ❌ 保留 /etc/fortune-mcp.env（含账号与 Bearer Token）
#   ❌ 保留 /var/lib/fortune-mcp（token 缓存）
#   ❌ 不改动防火墙规则
#
#   --purge            删除配置 + 缓存 + 运行用户，并关闭防火墙端口
#   --keep-app         保留程序目录（只停服务）
#   --close-firewall   只关闭放行的端口
#   --scope system|user|auto   服务作用域，默认 auto
#   --dir  <路径>      自定义安装目录
#   --port <端口>      自定义端口（清理防火墙与残留进程用）
# ---------------------------------------------------------------------------
set -euo pipefail

ORIG_ARGS=("$@")   # 供脚本搬迁后恢复原始参数

SERVICE="fortune-mcp"
PORT="${MCP_PORT:-18088}"
APP_DIR=""
SCOPE="auto"
ASSUME_YES=0
PURGE=0
KEEP_APP=0
CLOSE_FW=0
DRY=0

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1; shift ;;
        --purge) PURGE=1; CLOSE_FW=1; shift ;;
        --keep-app) KEEP_APP=1; shift ;;
        --close-firewall) CLOSE_FW=1; shift ;;
        --dry-run) DRY=1; shift ;;
        --scope) SCOPE="${2:-auto}"; shift 2 ;;
        --dir) APP_DIR="${2:-}"; shift 2 ;;
        --port) PORT="${2:-18088}"; shift 2 ;;
        -h|--help) awk '/^# -{10,}/{n++; if(n==2){print; exit}} n>=1 {print}' "$0"; exit 0 ;;
        *) echo "未知参数: $1（-h 查看用法）"; exit 1 ;;
    esac
done

info() { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" -eq 1 ]; then printf '    [dry-run] %s\n' "$*"; else "$@"; fi; }

IS_ROOT=0
if [ "$(id -u)" -eq 0 ]; then IS_ROOT=1; fi

if [ -z "$APP_DIR" ]; then
    if [ "$IS_ROOT" -eq 1 ]; then APP_DIR="/opt/fortune-mcp"; else APP_DIR="$HOME/fortune-mcp"; fi
fi

SYS_UNIT="/etc/systemd/system/${SERVICE}.service"
SYS_ENV="/etc/fortune-mcp.env"
SYS_DATA="/var/lib/fortune-mcp"
USER_UNIT="$HOME/.config/systemd/user/${SERVICE}.service"

# 若本脚本正位于待删除的安装目录内（例如 /opt/fortune-mcp/uninstall.sh），
# 先把自己复制到 /tmp 再执行，避免 bash 边读边执行时被自己删掉。
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ "$SELF_DIR" = "$APP_DIR" ] \
   && [ "${FORTUNE_UNINSTALL_RELOCATED:-0}" != "1" ]; then
    TMP_SELF="$(mktemp /tmp/fortune-mcp-uninstall.XXXXXX.sh)"
    cp -f "${BASH_SOURCE[0]}" "$TMP_SELF"
    export FORTUNE_UNINSTALL_RELOCATED=1
    exec bash "$TMP_SELF" "${ORIG_ARGS[@]}"
fi

# 搬迁后的临时副本退出时自动清理
case "${BASH_SOURCE[0]}" in
    /tmp/fortune-mcp-uninstall.*.sh) trap 'rm -f "$0"' EXIT ;;
esac

# ---------------------------------------------------------------- 探测安装形态
HAS_SYS_UNIT=0
if [ -f "$SYS_UNIT" ]; then HAS_SYS_UNIT=1; fi

HAS_USER_UNIT=0
if [ -f "$USER_UNIT" ]; then HAS_USER_UNIT=1; fi

HAS_APP_DIR=0
if [ -d "$APP_DIR" ]; then HAS_APP_DIR=1; fi

HAS_DOCKER=0
if [ "$HAS_APP_DIR" -eq 1 ] && [ -f "$APP_DIR/docker-compose.yml" ] && command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
        DQ="$(docker compose -f "$APP_DIR/docker-compose.yml" ps -q 2>/dev/null || true)"
    elif command -v docker-compose >/dev/null 2>&1; then
        DQ="$( cd "$APP_DIR" && docker-compose ps -q 2>/dev/null || true )"
    else
        DQ=""
    fi
    if [ -n "$DQ" ]; then HAS_DOCKER=1; fi
fi

if [ "$SCOPE" = "system" ]; then HAS_USER_UNIT=0; fi
if [ "$SCOPE" = "user" ]; then HAS_SYS_UNIT=0; fi

if [ "$HAS_SYS_UNIT" -eq 0 ] && [ "$HAS_USER_UNIT" -eq 0 ] && [ "$HAS_APP_DIR" -eq 0 ]; then
    warn "未发现 ${SERVICE} 的安装痕迹（单元文件与目录都不存在）"
    warn "如果装在别处，请用 --dir <安装目录> 指定"
    exit 0
fi

# ---------------------------------------------------------------- 确认
APP_STATE="(不存在)"
if [ "$HAS_APP_DIR" -eq 1 ]; then APP_STATE="(存在)"; fi

echo
echo "将要卸载 Fortune 记账 MCP Server："
echo "  安装目录    : $APP_DIR $APP_STATE"
if [ "$HAS_SYS_UNIT" -eq 1 ]; then  echo "  系统服务    : $SYS_UNIT"; fi
if [ "$HAS_USER_UNIT" -eq 1 ]; then echo "  用户级服务  : $USER_UNIT"; fi
if [ "$HAS_DOCKER" -eq 1 ]; then    echo "  容器        : docker compose 项目"; fi
echo "  监听端口    : ${PORT}/tcp"
if [ "$KEEP_APP" -eq 1 ]; then
    echo "  程序目录    : 保留（仅停服务）"
else
    echo "  程序目录    : 删除"
fi
if [ "$PURGE" -eq 1 ]; then
    echo "  配置与数据  : 删除（$SYS_ENV / $SYS_DATA / $APP_DIR/.env）"
else
    echo "  配置与数据  : 保留（$SYS_ENV / $SYS_DATA）"
fi
if [ "$CLOSE_FW" -eq 1 ]; then
    echo "  防火墙      : 关闭 ${PORT}/tcp"
else
    echo "  防火墙      : 不改动"
fi
echo

if [ "$DRY" -eq 1 ]; then
    warn "dry-run 模式：只打印动作，不会真的执行"
elif [ "$ASSUME_YES" -eq 0 ]; then
    if [ ! -t 0 ]; then
        die "当前为非交互环境，请加 --yes 明确确认（可先用 --dry-run 预览）"
    fi
    printf '确认卸载？输入 yes 继续：'
    read -r ANSWER
    case "$ANSWER" in
        yes|YES|y|Y) ;;
        *) echo "已取消，未做任何改动"; exit 1 ;;
    esac
fi

# ---------------------------------------------------------------- 停止服务
if [ "$HAS_SYS_UNIT" -eq 1 ]; then
    if [ "$IS_ROOT" -eq 1 ]; then
        info "停止并禁用系统服务"
        run systemctl stop "$SERVICE" >/dev/null 2>&1 || true
        run systemctl disable "$SERVICE" >/dev/null 2>&1 || true
        run rm -f "$SYS_UNIT"
        run systemctl daemon-reload >/dev/null 2>&1 || true
        run systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
    else
        warn "系统服务需要 root 权限，请用 sudo 重新执行：sudo bash $0 --yes"
    fi
fi

if [ "$HAS_USER_UNIT" -eq 1 ]; then
    info "停止并禁用用户级服务"
    run systemctl --user stop "$SERVICE" >/dev/null 2>&1 || true
    run systemctl --user disable "$SERVICE" >/dev/null 2>&1 || true
    run rm -f "$USER_UNIT"
    run systemctl --user daemon-reload >/dev/null 2>&1 || true
    run systemctl --user reset-failed "$SERVICE" >/dev/null 2>&1 || true
    loginctl disable-linger "$(id -un)" >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- Docker
if [ "$HAS_DOCKER" -eq 1 ]; then
    info "停止容器"
    if docker compose version >/dev/null 2>&1; then
        if [ "$PURGE" -eq 1 ]; then
            run docker compose -f "$APP_DIR/docker-compose.yml" down -v --rmi local
        else
            run docker compose -f "$APP_DIR/docker-compose.yml" down
        fi
    elif command -v docker-compose >/dev/null 2>&1; then
        if [ "$PURGE" -eq 1 ]; then
            ( cd "$APP_DIR" && run docker-compose down -v --rmi local )
        else
            ( cd "$APP_DIR" && run docker-compose down )
        fi
    fi
fi

# ---------------------------------------------------------------- 残留进程
PIDS=""
if command -v pgrep >/dev/null 2>&1; then
    PIDS="$(pgrep -f "${APP_DIR}/server.py" 2>/dev/null || true)"
fi
if [ -z "$PIDS" ] && [ -r /proc ] && command -v ss >/dev/null 2>&1; then
    LISTEN_PID="$(ss -lptnH "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)"
    if [ -n "${LISTEN_PID:-}" ] && grep -qa "server.py" "/proc/${LISTEN_PID}/cmdline" 2>/dev/null; then
        PIDS="$LISTEN_PID"
    fi
fi
if [ -n "$PIDS" ]; then
    info "结束残留进程: $(echo "$PIDS" | tr '\n' ' ')"
    for pid in $PIDS; do run kill "$pid" >/dev/null 2>&1 || true; done
    sleep 1
    for pid in $PIDS; do
        if kill -0 "$pid" >/dev/null 2>&1; then
            run kill -9 "$pid" >/dev/null 2>&1 || true
        fi
    done
else
    info "没有发现残留进程"
fi

# ---------------------------------------------------------------- 防火墙
if [ "$CLOSE_FW" -eq 1 ]; then
    if [ "$IS_ROOT" -eq 1 ]; then
        if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
            run firewall-cmd --permanent --remove-port="${PORT}/tcp" >/dev/null 2>&1 || true
            run firewall-cmd --reload >/dev/null 2>&1 || true
            info "firewalld 已关闭 ${PORT}/tcp"
        elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
            run ufw delete allow "${PORT}/tcp" >/dev/null 2>&1 || true
            info "ufw 已关闭 ${PORT}/tcp"
        else
            warn "未检测到活动防火墙，跳过端口清理"
        fi
    else
        warn "关闭防火墙端口需要 root，已跳过"
    fi
fi

# ---------------------------------------------------------------- 文件
ENV_BACKUP=""
if [ "$KEEP_APP" -eq 1 ]; then
    info "按 --keep-app 保留程序目录 $APP_DIR"
    rm -rf "$APP_DIR/__pycache__" 2>/dev/null || true
elif [ "$HAS_APP_DIR" -eq 1 ]; then
    # 用户级安装把配置放在 $APP_DIR/.env，删目录前先备份，避免“保留配置”变成空话
    if [ "$PURGE" -eq 0 ] && [ -f "$APP_DIR/.env" ]; then
        ENV_BACKUP="${APP_DIR}.env.bak"
        run cp -f "$APP_DIR/.env" "$ENV_BACKUP"
        info "配置已备份到 $ENV_BACKUP"
    fi
    info "删除程序目录 $APP_DIR"
    run rm -rf "$APP_DIR"
fi

if [ "$PURGE" -eq 1 ]; then
    info "删除配置与缓存"
    run rm -f "$SYS_ENV"
    if [ -d "$SYS_DATA" ]; then run rm -rf "$SYS_DATA"; fi
    if [ -f "$APP_DIR/.env" ]; then run rm -f "$APP_DIR/.env"; fi
    if [ "$IS_ROOT" -eq 1 ] && id -u fortune >/dev/null 2>&1; then
        if [ "$DRY" -eq 1 ]; then
            echo "    [dry-run] userdel fortune"
        elif userdel fortune 2>/dev/null; then
            info "已删除运行用户 fortune"
        else
            warn "运行用户 fortune 删除失败（可能仍有文件属主），可手动 userdel fortune"
        fi
    fi
fi

# ---------------------------------------------------------------- 汇总
echo
if [ "$DRY" -eq 1 ]; then
    info "dry-run 结束，未做任何改动"
    exit 0
fi

info "卸载完成"
if [ "$KEEP_APP" -eq 1 ]; then APP_LINE="· （程序目录已保留：$APP_DIR）"; else APP_LINE="· 程序目录 $APP_DIR"; fi
if [ "$PURGE" -eq 1 ]; then DATA_LINE="· 配置 $SYS_ENV 与缓存 $SYS_DATA"; else DATA_LINE="· （配置与缓存已保留）"; fi

cat <<EOF

 已清理
   · 服务进程与 systemd 单元
   ${APP_LINE}
   ${DATA_LINE}

 仍保留（彻底清空请再执行 --purge）
   · ${SYS_ENV}
   · ${SYS_DATA}
EOF
if [ -n "$ENV_BACKUP" ]; then
cat <<EOF
   · ${ENV_BACKUP}（原 ${APP_DIR}/.env 的备份）
EOF
fi
cat <<EOF
   · 日志：journalctl -u ${SERVICE}
     （清理：journalctl --rotate && journalctl --vacuum-time=1s）

 提示
   · 别忘了到 WorkBuddy 企业版后台把「Fortune 记账」连接器停用或下架，
     否则平台侧仍会反复尝试连接并报超时。
   · 重新安装：cd <安装包目录> && sudo bash install.sh
EOF
