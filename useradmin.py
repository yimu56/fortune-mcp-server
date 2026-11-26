#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多租户凭据库管理工具（平台用户 ↔ 记账账号）。

三种用法：

1) 交互式菜单（推荐新手）——不用记参数，跟着提示走
       sudo python3 /opt/fortune-mcp/useradmin.py menu

2) 交互式向导（只加一个人）
       sudo python3 /opt/fortune-mcp/useradmin.py add
       # 会依次问：平台用户、记账账号、密码、接口地址、备注，并可选立即登录验证

3) 脚本化（适合批量/自动化，CI 里用）
       echo '记账密码' | sudo python3 /opt/fortune-mcp/useradmin.py add \\
           --user demo_user --fortune-user 示例用户 --note '示例用户/研发部' --password-stdin
       sudo python3 /opt/fortune-mcp/useradmin.py list
       sudo python3 /opt/fortune-mcp/useradmin.py disable --user demo_user
       sudo python3 /opt/fortune-mcp/useradmin.py remove  --user demo_user
       sudo python3 /opt/fortune-mcp/useradmin.py import  users.csv
       sudo python3 /opt/fortune-mcp/useradmin.py template > users.csv

公共参数：
    --file      指定 users.json 路径（默认取 FORTUNE_USERS_FILE 或 /var/lib/fortune-mcp/users.json）
    --key       指定加密密钥（默认取 FORTUNE_CRED_KEY，其次同目录 users.json.key）
    --env-file  指定环境变量文件（默认依次尝试 FORTUNE_ENV_FILE、/etc/fortune-mcp.env、同目录 .env）

说明：
    · 启动时自动加载环境变量文件，行为与 systemd 的 EnvironmentFile 一致；
      已存在的环境变量不会被覆盖，因此 shell 里 export 的值优先。
    · 密码优先用 getpass 输入（不回显）；自动化时可用 --password-stdin 或 USERADMIN_PLAIN=1。
    · 改了凭据库不需要重启服务（每次调用都重新读取）。
"""

import argparse
import getpass
import json
import os
import sys

from user_store import DEFAULT_USERS_FILE, UserStore, UserStoreError


# ---------------------------------------------------------------------------
# 环境变量文件加载
# ---------------------------------------------------------------------------
DEFAULT_ENV_FILES = (
    os.environ.get("FORTUNE_ENV_FILE") or "/etc/fortune-mcp.env",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    os.path.join(os.getcwd(), ".env"),
)


def _parse_env_line(line):
    """解析一行 KEY=VALUE，返回 (key, value)；非赋值行返回 None。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[7:].lstrip()
    if "=" not in text:
        return None
    key, value = text.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env_files(explicit=None):
    """把环境变量文件读入 os.environ，已存在的变量保持原值。

    与 server.py 中的同名函数保持解析规则、优先级一致，改动需同步。
    服务端由 systemd 的 EnvironmentFile 注入配置；本 CLI 为手工执行，
    需要自行加载，否则手工校验账号时读不到 FORTUNE_BASE_URL。
    """
    candidates = []
    if explicit:
        path = os.path.abspath(explicit)
        if not os.path.isfile(path):
            raise UserStoreError("环境变量文件不存在：%s" % path)
        candidates.append(path)
    candidates.extend(DEFAULT_ENV_FILES)

    loaded, seen = [], set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    parsed = _parse_env_line(line)
                    if parsed and parsed[0] not in os.environ:
                        os.environ[parsed[0]] = parsed[1]
        except OSError:
            continue
        loaded.append(path)
    return loaded


# ---------------------------------------------------------------------------
# 基础交互
# ---------------------------------------------------------------------------
def ask(prompt, default=None, required=True):
    """读一个值；default 不为 None 时回车即取默认值。"""
    suffix = " [%s]" % default if default else ""
    while True:
        try:
            value = input("%s%s：" % (prompt, suffix)).strip()
        except EOFError:
            raise UserStoreError("输入结束，已取消")
        if not value and default is not None:
            return default
        if value:
            return value
        if not required:
            return ""
        print("  不能为空，请重新输入")


def confirm(prompt, default=True):
    tip = "Y/n" if default else "y/N"
    try:
        answer = input("%s [%s]：" % (prompt, tip)).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "是", "1")


def ask_password():
    """密码输入：默认 getpass 不回显；USERADMIN_PLAIN=1 时正常回显（自动化用）。"""
    if os.environ.get("USERADMIN_PLAIN") == "1":
        return input("记账密码（回显模式）：").strip()
    try:
        return getpass.getpass("记账密码（输入不显示）：")
    except Exception:
        return input("记账密码（无法隐藏输入，将回显）：").strip()


def show_user_line(name, record, store):
    state = "启用" if record.get("enabled", True) else "停用"
    return "  %-24s → %-14s [%s] %s" % (
        name, record.get("fortune_username"), state, record.get("note") or "")


# ---------------------------------------------------------------------------
# 账号可用性验证
# ---------------------------------------------------------------------------
def verify_credential(platform_user, store):
    """用刚保存的凭据真登一次，确认映射可用。"""
    try:
        from fortune_api import FortuneClient
    except Exception as exc:  # noqa: BLE001
        print("   (跳过验证：无法加载 fortune_api：%s)" % exc)
        return
    try:
        creds = store.creds_for(platform_user)
        if not creds.get("base_url"):
            print("   ⚠ 未读到接口地址，将使用内置默认地址；"
                  "建议在 /etc/fortune-mcp.env 设置 FORTUNE_BASE_URL，或用 --base-url 指定")
        client = FortuneClient(base_url=creds["base_url"], username=creds["username"],
                               password=creds["password"])
        client.login()
        gid = client.get_default_group_id()
        group = client.get_group(gid) or {}
        books = client.get_books(gid) or []
        accounts = client.get_accounts(gid) or []
        print("   ✔ 登录成功：分组 %s（%s）、账本「%s」、账户 %d 个"
              % (gid, group.get("groupName") or "-",
                 (books[0].get("bookName") if books else "-"), len(accounts)))
        if not accounts:
            print("     提示：该账套还没有账户，员工记账时会提示先建账户")
    except Exception as exc:  # noqa: BLE001
        print("   ✗ 登录失败：%s" % exc)
        print("     映射已保存，但账号密码可能有误。修正：useradmin.py add --user %s ..."
              % platform_user)


# ---------------------------------------------------------------------------
# 向导式添加（一个人）
# ---------------------------------------------------------------------------
def wizard_add(store, args):
    print("\n" + "=" * 58)
    print(" 添加用户映射")
    print("=" * 58)
    print(" 平台用户  = 企业后台传过来的身份值（工号 / 邮箱前缀 / 登录名）")
    print(" 记账账号  = Fortune 记账系统里的登录名（如 示例用户）")
    print(" 提示：先跑一次 fortune_diagnostic 看清平台的 platformUser 再填，最稳。\n")

    added = 0
    while True:
        platform_user = getattr(args, "user", None) or ask("平台用户标识（如 demo_user / 10086）")
        exist = store.get(platform_user)
        if exist:
            print("   ⚠ 该用户已存在：" + show_user_line(platform_user, exist, store).strip())
            if not confirm("   覆盖它？", default=False):
                if not confirm("继续添加另一个用户？", default=True):
                    break
                continue

        fortune_user = getattr(args, "fortune_user", None) or ask("记账账号（如 示例用户）")
        password = read_password(args)
        base_url = (getattr(args, "base_url", None)
                    or ask("专用接口地址（回车用全局配置）", default="", required=False) or None)
        note = (getattr(args, "note", None)
                or ask("备注（姓名/部门，可空）", default="", required=False))

        print("\n  ┌ 请确认 ─────────────────────────────")
        print("  │ 平台用户 : %s" % platform_user)
        print("  │ 记账账号 : %s" % fortune_user)
        print("  │ 密码     : %s（%d 位）" % ("*" * min(len(password), 10), len(password)))
        print("  │ 接口地址 : %s" % (base_url or "（全局默认）"))
        print("  │ 备注     : %s" % (note or "-"))
        print("  └─────────────────────────────────────")

        if not confirm("保存这条映射？", default=True):
            if not confirm("继续添加下一个？", default=True):
                break
            continue

        store.upsert(platform_user, fortune_user, password,
                     base_url=base_url, note=note)
        added += 1
        print("✅ 已保存：%s → %s" % (platform_user, fortune_user))

        if confirm("现在验证这个账号能否登录？", default=True):
            verify_credential(platform_user, store)

        if not confirm("继续添加下一个？", default=False):
            break
        print()

    # 收尾提示
    total = len(store.platform_users())
    print("\n" + "-" * 58)
    print("本次添加/更新 %d 条，凭据库现有 %d 个用户" % (added, total))
    print("凭据库：%s" % store.path)
    if total and not _multi_tenant_enabled():
        print("\n⚠ 提醒：多租户开关似乎还没打开。请在环境配置里设置")
        print("   FORTUNE_MULTI_TENANT=1 然后 systemctl restart fortune-mcp")
    return 0


def _multi_tenant_enabled():
    value = (os.environ.get("FORTUNE_MULTI_TENANT") or "").strip()
    return value == "1"


# ---------------------------------------------------------------------------
# 菜单式管理
# ---------------------------------------------------------------------------
def menu(store):
    while True:
        users = store.platform_users()
        print("\n" + "=" * 58)
        print(" Fortune 记账 · 多用户管理")
        print(" 凭据库：%s" % store.path)
        print(" 当前 %d 个用户：%s" % (len(users), "、".join(users) if users else "（空）"))
        print("=" * 58)
        print("  1) 添加 / 修改用户")
        print("  2) 查看全部用户")
        print("  3) 停用 / 启用用户")
        print("  4) 删除用户")
        print("  5) 验证某账号能否登录")
        print("  6) CSV 批量导入")
        print("  0) 退出")
        try:
            choice = input("\n请选择：").strip()
        except EOFError:
            print()
            return 0

        if choice == "0":
            print("再见。")
            return 0

        if choice == "1":
            wizard_add(store, argparse.Namespace(
                user=None, fortune_user=None, password=None, password_stdin=False,
                base_url=None, note=None))

        elif choice == "2":
            cmd_list(argparse.Namespace(json=False), store)

        elif choice in ("3", "4", "5"):
            if not users:
                print("  凭据库还是空的，先添加用户。")
                continue
            name = ask("要操作哪个用户（输入上面的名字）")
            record = store.get(name)
            if record is None:
                print("  ✗ 没找到：%s" % name)
                continue

            if choice == "3":
                state = record.get("enabled", True)
                print(show_user_line(name.lower(), record, store))
                if confirm("当前是「%s」，要改成「%s」吗？"
                           % ("启用" if state else "停用", "停用" if state else "启用")):
                    store.set_enabled(name, not state)
                    print("✅ 已%s：%s" % ("停用" if state else "启用", name))
                    if state:
                        print("   该用户下次调用会收到「账号已被停用」；重新启用用同一入口。")

            elif choice == "4":
                print(show_user_line(name.lower(), record, store))
                if confirm("确认删除？此操作不可恢复（可先用停用代替）", default=False):
                    store.remove(name)
                    print("✅ 已删除：%s" % name)

            elif choice == "5":
                print("  正在验证 %s …" % name)
                verify_credential(name.lower(), store)

        elif choice == "6":
            path = ask("CSV 文件路径")
            try:
                added, updated, failed = store.import_csv(path)
                print("导入完成：新增 %d，更新 %d，失败 %d" % (added, updated, len(failed)))
                for line in failed:
                    print("  ✗ %s" % line)
            except Exception as exc:  # noqa: BLE001
                print("  ✗ 导入失败：%s" % exc)

        else:
            print("  无效选择，请输入 0-6。")


# ---------------------------------------------------------------------------
# 命令行子命令实现
# ---------------------------------------------------------------------------
def build_store(args):
    path = getattr(args, "file", None) or os.environ.get("FORTUNE_USERS_FILE") or DEFAULT_USERS_FILE
    return UserStore(path=path,
                     key_text=getattr(args, "key", None) or os.environ.get("FORTUNE_CRED_KEY"))


def read_password(args):
    if getattr(args, "password_stdin", False):
        data = sys.stdin.read().strip()
        if not data:
            raise UserStoreError("标准输入为空，未读到密码")
        return data
    if getattr(args, "password", None):
        return args.password
    first = ask_password()
    if not first:
        raise UserStoreError("密码不能为空")
    if os.environ.get("USERADMIN_PLAIN") != "1":
        if getpass.getpass("再输入一次：") != first:
            raise UserStoreError("两次输入不一致")
    return first


def cmd_add(args, store):
    password = read_password(args)
    key = store.upsert(args.user, args.fortune_user, password,
                       base_url=args.base_url, note=args.note,
                       enabled=not args.disabled)
    print("已保存：平台用户 %s → 记账账号 %s%s"
          % (key, args.fortune_user, "（已停用）" if args.disabled else ""))
    print("凭据库：%s" % store.path)
    if args.verify:
        verify_credential(key, store)
    return 0


def cmd_list(args, store):
    users = store.platform_users()
    if args.json:
        out = []
        for name in users:
            record = store.get(name)
            out.append({"platform_user": name,
                        "fortune_username": record.get("fortune_username"),
                        "enabled": record.get("enabled", True),
                        "note": record.get("note"),
                        "updated": record.get("updated")})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not users:
        print("凭据库为空：%s" % store.path)
        return 0
    print("%-24s %-16s %-6s %-20s %s" % ("平台用户", "记账账号", "状态", "备注", "更新时间"))
    print("-" * 88)
    for name in users:
        record = store.get(name)
        print("%-24s %-16s %-6s %-20s %s"
              % (name, record.get("fortune_username"),
                 "启用" if record.get("enabled", True) else "停用",
                 (record.get("note") or "")[:20], record.get("updated") or ""))
    print("\n共 %d 个用户；凭据库：%s" % (len(users), store.path))
    return 0


def cmd_toggle(args, store, enabled):
    store.set_enabled(args.user, enabled)
    print("%s 已%s" % (args.user, "启用" if enabled else "停用"))
    return 0


def cmd_remove(args, store):
    store.remove(args.user)
    print("已删除：%s" % args.user)
    return 0


def cmd_import(args, store):
    added, updated, failed = store.import_csv(args.csv)
    print("导入完成：新增 %d，更新 %d，失败 %d" % (added, updated, len(failed)))
    for line in failed:
        print("  ✗ %s" % line)
    return 0 if not failed else 1


def cmd_template(args, store):
    print("platform_user,fortune_username,password,note")
    print("demo_user,示例用户,记账密码,示例用户/研发部")
    print("demo_user2,示例用户2,记账密码,示例用户2/市场部")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="多租户凭据库管理（交互式：不加子命令参数即进入向导）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例：
  useradmin.py menu                        # 交互式菜单（推荐）
  useradmin.py add                         # 向导式添加，逐项询问
  useradmin.py add --user demo_user --fortune-user 示例用户   # 半交互（密码仍需输入）
  echo '密码' | useradmin.py add --user demo_user --fortune-user 示例用户 --password-stdin
  useradmin.py list --json
""")
    parser.add_argument("--file", help="users.json 路径")
    parser.add_argument("--key", help="加密密钥（hex 或 base64）")
    parser.add_argument("--env-file", help="环境变量文件（默认 /etc/fortune-mcp.env 或 .env）")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="添加/更新用户（缺参数时进入向导）")
    p_add.add_argument("--user", help="平台用户标识（如工号、邮箱前缀）")
    p_add.add_argument("--fortune-user", help="该人的记账系统账号")
    p_add.add_argument("--password", help="记账密码（不推荐，会留在 shell 历史）")
    p_add.add_argument("--password-stdin", action="store_true", help="从标准输入读密码")
    p_add.add_argument("--base-url", help="该账号专用的接口地址（默认全局配置）")
    p_add.add_argument("--note", help="备注，如姓名/部门")
    p_add.add_argument("--disabled", action="store_true", help="先创建但停用")
    p_add.add_argument("--verify", action="store_true", help="保存后立即验证能否登录")

    for name, help_text in (("enable", "启用用户"), ("disable", "停用用户"),
                            ("remove", "删除用户")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--user", required=True)

    p_list = sub.add_parser("list", help="列出全部映射")
    p_list.add_argument("--json", action="store_true")

    p_import = sub.add_parser("import", help="从 CSV 批量导入")
    p_import.add_argument("csv")

    sub.add_parser("template", help="打印 CSV 模板")
    sub.add_parser("menu", aliases=["interactive", "i"], help="交互式菜单管理")

    args = parser.parse_args()
    try:
        load_env_files(getattr(args, "env_file", None))
        store = build_store(args)
        cmd = args.cmd

        if cmd in (None, "menu", "interactive", "i"):
            return menu(store)
        if cmd == "add":
            if not args.user or not args.fortune_user:
                if not sys.stdin.isatty():
                    raise UserStoreError(
                        "缺少 --user / --fortune-user。\n"
                        "  脚本里请补全参数；想交互添加请直接运行：useradmin.py menu")
                return wizard_add(store, args)
            return cmd_add(args, store)
        if cmd == "list":
            return cmd_list(args, store)
        if cmd == "enable":
            return cmd_toggle(args, store, True)
        if cmd == "disable":
            return cmd_toggle(args, store, False)
        if cmd == "remove":
            return cmd_remove(args, store)
        if cmd == "import":
            return cmd_import(args, store)
        if cmd == "template":
            return cmd_template(args, store)
        parser.print_help()
        return 1
    except UserStoreError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
