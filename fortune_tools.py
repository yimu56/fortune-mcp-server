#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fortune 记账 MCP 工具定义与实现（被 server.py 的 HTTP / stdio 两种传输共用）。"""

from datetime import datetime

from fortune_api import FortuneError

SERVER_NAME = "fortune-bookkeeping"
SERVER_VERSION = "3.0.0"

INSTRUCTIONS = """\
本服务连接 Fortune 记账系统，可查询账户余额、账本、分类、收付款方与标签，并新增支出、收入、转账与账户。

使用约定：
1. 记账前先调用 fortune_context 拿到 groupId / bookId / 账户列表与余额，避免用错账户。
2. account_id / category_id 既可以传数字 ID，也可以直接传中文名称，工具会自动解析；名称模糊命中多个时先向用户确认。
3. bill_type：1=支出，2=收入，3=转账；trade_time 省略时取服务器当前时间，格式 YYYY-MM-DD HH:MM:SS。
4. 记账类工具执行后账户余额会立即变化，属于写入操作；用户未明确表示要记账时不要自动调用。
5. 金额单位与账本币种一致（默认 CNY），amount 必须大于 0。
"""


# ---------------------------------------------------------------------------
# 名称 / ID 解析辅助
# ---------------------------------------------------------------------------
def _as_id(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_group(client, group_id=None):
    if group_id is not None:
        gid = _as_id(group_id)
        if gid is None:
            raise FortuneError(f"无法识别 group_id: {group_id}")
        return gid
    return client.get_default_group_id()


def _resolve_book(client, group_id, value=None):
    books = client.get_books(group_id) or []
    if value is not None:
        bid = _as_id(value)
        if bid is not None:
            match = next((b for b in books if b.get("bookId") == bid), None)
            if match is None:
                raise FortuneError(
                    "账本 %s 不属于当前用户的当前分组；可用账本：%s"
                    % (value, ", ".join("%s(%s)" % (b.get("bookName"), b.get("bookId"))
                                        for b in books) or "无"))
            return bid, match
        for b in books:
            if str(value) == b.get("bookName"):
                return b["bookId"], b
        raise FortuneError(f"未找到账本: {value}")
    if not books:
        raise FortuneError(f"分组 {group_id} 下没有可用账本")
    return books[0]["bookId"], books[0]


def _resolve_account(client, group_id, value):
    accounts = client.get_accounts(group_id) or []
    aid = _as_id(value)
    if aid is not None:
        match = next((a for a in accounts if a.get("accountId") == aid), None)
        if match is None:
            # 关键安全校验：下游服务端不校验账户归属，必须在代理层拦住跨用户写入
            raise FortuneError(
                "账户 %s 不属于当前用户；可用账户：%s"
                % (value, ", ".join("%s(%s)" % (a.get("accountName"), a.get("accountId"))
                                    for a in accounts) or "无"))
        return aid, match
    text = str(value or "").strip()
    if not text:
        raise FortuneError("account_id 不能为空")
    for a in accounts:
        if text == a.get("accountName"):
            return a["accountId"], a
    for a in accounts:
        if text in str(a.get("accountName", "")):
            return a["accountId"], a
    raise FortuneError(f"未找到账户: {value}；可用账户: "
                       + ", ".join(str(a.get("accountName")) for a in accounts))


def _flatten_categories(nodes):
    result = []
    for node in nodes or []:
        result.append(node)
        result.extend(_flatten_categories(node.get("children")))
    return result


def _resolve_category(client, book_id, bill_type, value):
    categories = _flatten_categories(client.get_categories(book_id, bill_type))
    cid = _as_id(value)
    if cid is not None:
        if not any(c.get("categoryId") == cid for c in categories):
            raise FortuneError(
                "分类 %s 不属于当前账本；可用分类：%s"
                % (value, ", ".join("%s(%s)" % (c.get("categoryName"), c.get("categoryId"))
                                    for c in categories) or "无"))
        return cid
    text = str(value or "").strip()
    for c in categories:
        if text and text == c.get("categoryName"):
            return c["categoryId"]
    for c in categories:
        if text and text in str(c.get("categoryName", "")):
            return c["categoryId"]
    names = ", ".join(str(c.get("categoryName")) for c in categories)
    raise FortuneError(f"未找到分类: {value}；可用分类: {names}")


def _validate_tags(client, book_id, bill_type, tag_ids):
    """校验标签属于当前账本，防止跨账套引用。"""
    if not tag_ids:
        return []
    valid = {t.get("tagId") for t in _flatten_categories(client.get_tags(book_id, bill_type))}
    bad = [t for t in tag_ids if t not in valid]
    if bad:
        raise FortuneError("标签 %s 不属于当前账本；可用标签：%s"
                           % (bad, ", ".join(str(v) for v in sorted(valid)) or "无"))
    return tag_ids


def _validate_payee(client, book_id, bill_type, payee_id):
    """校验收付款方属于当前账本。"""
    if payee_id is None:
        return None
    payees = client.get_payees(book_id, bill_type) or []
    if not any(p.get("payeeId") == payee_id for p in payees):
        raise FortuneError("收付款方 %s 不属于当前账本；可用：%s"
                           % (payee_id,
                              ", ".join(str(p.get("payeeName")) for p in payees) or "无"))
    return payee_id


def _positive_amount(value):
    if value is None or value == "":
        raise FortuneError("amount 不能为空")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise FortuneError(f"amount 必须是数字，收到: {value!r}")
    if amount <= 0:
        raise FortuneError("amount 必须大于 0")
    return round(amount, 2)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def tool_login(client, args):
    token = client.login(force=bool(args.get("force", False)))
    info = ((client.user_info or {}).get("userInfo") or {})
    return {"login": "ok",
            "username": info.get("userName") or client.username,
            "nickname": info.get("nickName"),
            "baseUrl": client.base_url,
            "tokenPrefix": (token or "")[:16] + "..."}


def tool_context(client, args):
    client.ensure_login()
    gid = _resolve_group(client, args.get("group_id"))
    book_id, book = _resolve_book(client, gid, args.get("book_id"))
    accounts = client.get_accounts(gid) or []
    group = client.get_group(gid) or {}
    return {
        "groupId": gid,
        "groupName": group.get("groupName"),
        "bookId": book_id,
        "bookName": (book or {}).get("bookName"),
        "defaultCurrency": (book or {}).get("defaultCurrency"),
        "accounts": [
            {"accountId": a.get("accountId"), "accountName": a.get("accountName"),
             "balance": a.get("balance"), "currencyCode": a.get("currencyCode"),
             "canExpense": a.get("canExpense"), "canIncome": a.get("canIncome"),
             "canTransferOut": a.get("canTransferOut"),
             "canTransferIn": a.get("canTransferIn")}
            for a in accounts
        ],
    }


def tool_list_groups(client, args):
    client.ensure_login()
    return {"groups": client.get_groups() or []}


def tool_list_books(client, args):
    client.ensure_login()
    gid = _resolve_group(client, args.get("group_id"))
    return {"groupId": gid, "books": client.get_books(gid) or []}


def tool_list_accounts(client, args):
    client.ensure_login()
    gid = _resolve_group(client, args.get("group_id"))
    accounts = client.get_accounts(gid) or []
    total = sum(float(a.get("balance") or 0) for a in accounts)
    return {"groupId": gid, "count": len(accounts),
            "totalBalance": round(total, 2), "accounts": accounts}


def tool_add_account(client, args):
    client.ensure_login()
    gid = _resolve_group(client, args.get("group_id"))
    name = str(args.get("account_name") or "").strip()
    if not name:
        raise FortuneError("account_name 不能为空")
    result = client.add_account(
        group_id=gid, account_name=name,
        balance=float(args.get("balance") or 0),
        currency_code=str(args.get("currency_code") or "CNY"),
        account_type=int(args.get("account_type") or 1),
        card_no=str(args.get("card_no") or ""),
    )
    accounts = client.get_accounts(gid) or []
    created = next((a for a in accounts if a.get("accountName") == name), None)
    return {"result": result.get("msg", "ok"), "groupId": gid, "account": created}


def _list_options(client, args, kind):
    client.ensure_login()
    gid = _resolve_group(client, args.get("group_id"))
    book_id, _ = _resolve_book(client, gid, args.get("book_id"))
    bill_type = int(args.get("bill_type") or 1)
    if bill_type not in (1, 2, 3):
        raise FortuneError("bill_type 必须是 1(支出)/2(收入)/3(转账)")
    if kind == "categories":
        data = client.get_categories(book_id, bill_type)
    elif kind == "payees":
        data = client.get_payees(book_id, bill_type)
    else:
        data = client.get_tags(book_id, bill_type)
    return {"bookId": book_id, "billType": bill_type, kind: data or []}


def tool_list_categories(client, args):
    return _list_options(client, args, "categories")


def tool_list_payees(client, args):
    return _list_options(client, args, "payees")


def tool_list_tags(client, args):
    return _list_options(client, args, "tags")


def _add_bill_common(client, args, bill_type):
    client.ensure_login()
    gid = _resolve_group(client, args.get("group_id"))
    book_id, _ = _resolve_book(client, gid, args.get("book_id"))
    title = str(args.get("title") or "").strip()
    if not title:
        raise FortuneError("title 不能为空")
    amount = _positive_amount(args.get("amount"))
    trade_time = str(args.get("trade_time") or _now())

    if bill_type == 3:
        from_id, from_info = _resolve_account(client, gid, args.get("account_id"))
        to_id, to_info = _resolve_account(client, gid, args.get("to_account_id"))
        if from_id == to_id:
            raise FortuneError("转出账户与转入账户不能相同")
        result = client.add_transfer(book_id, title, amount, trade_time,
                                     from_id, to_id)
        detail = {"fromAccount": (from_info or {}).get("accountName"),
                  "fromAccountId": from_id,
                  "toAccount": (to_info or {}).get("accountName"),
                  "toAccountId": to_id}
    else:
        account_id, account_info = _resolve_account(client, gid, args.get("account_id"))
        category_value = args.get("category_id") or args.get("category_name")
        if not category_value:
            raise FortuneError("category_id 或 category_name 不能为空")
        category_id = _resolve_category(client, book_id, bill_type, category_value)
        tag_ids = args.get("tag_ids") or []
        if isinstance(tag_ids, (str, int)):
            tag_ids = [tag_ids]
        tag_ids = [_as_id(t) for t in tag_ids if _as_id(t) is not None]
        tag_ids = _validate_tags(client, book_id, bill_type, tag_ids)
        payee_id = _validate_payee(client, book_id, bill_type,
                                   _as_id(args.get("payee_id")))
        result = client.add_bill(
            book_id, bill_type, title, trade_time, account_id,
            [{"categoryId": category_id, "amount": amount}],
            tag_id_list=tag_ids, payee_id=payee_id)
        detail = {"account": (account_info or {}).get("accountName"),
                  "accountId": account_id,
                  "categoryId": category_id,
                  "billType": "支出" if bill_type == 1 else "收入"}

    accounts = client.get_accounts(gid) or []
    balances = {str(a.get("accountName")): a.get("balance") for a in accounts}
    return {"result": result.get("msg", "ok"), "billType": bill_type,
            "title": title, "amount": amount, "tradeTime": trade_time,
            "bookId": book_id, **detail, "balancesAfter": balances}


def tool_add_expense(client, args):
    return _add_bill_common(client, args, 1)


def tool_add_income(client, args):
    return _add_bill_common(client, args, 2)


def tool_add_transfer(client, args):
    return _add_bill_common(client, args, 3)


def tool_add_bill(client, args):
    """高级通用接口：直接传原始账单字段（支持多分类分摊、orderId）。

    所有 ID 都会先校验归属（账本/账户/分类/标签/收付款方），
    避免把别人的数据 ID 混进来。
    """
    client.ensure_login()
    required = ["book_id", "bill_type", "title", "trade_time", "account_id"]
    missing = [k for k in required if args.get(k) is None]
    if missing:
        raise FortuneError("缺少参数: " + ", ".join(missing))
    bill_type = int(args["bill_type"])
    if bill_type not in (1, 2, 3):
        raise FortuneError("bill_type 必须是 1(支出)/2(收入)/3(转账)")

    gid = _resolve_group(client, args.get("group_id"))
    book_id, _ = _resolve_book(client, gid, int(args["book_id"]))
    account_id, _ = _resolve_account(client, gid, int(args["account_id"]))

    to_account_id = None
    if args.get("to_account_id") is not None:
        to_account_id, _ = _resolve_account(client, gid, _as_id(args["to_account_id"]))
        if to_account_id == account_id:
            raise FortuneError("转出账户与转入账户不能相同")

    pairs = []
    amount = args.get("amount")
    if bill_type == 3:
        amount = _positive_amount(amount)
    else:
        raw_pairs = args.get("category_amount_pair") or []
        if not raw_pairs:
            raise FortuneError("支出/收入必须提供 category_amount_pair")
        for item in raw_pairs:
            item = item or {}
            cid = _as_id(item.get("categoryId"))
            if cid is None:
                raise FortuneError("category_amount_pair 中存在无效的 categoryId")
            _resolve_category(client, book_id, bill_type, cid)  # 归属校验
            pairs.append({"categoryId": cid,
                          "amount": _positive_amount(item.get("amount"))})
        amount = None

    tag_ids = [_as_id(t) for t in (args.get("tag_id_list") or [])
               if _as_id(t) is not None]
    tag_ids = _validate_tags(client, book_id, bill_type, tag_ids)
    payee_id = _validate_payee(client, book_id, bill_type, _as_id(args.get("payee_id")))

    result = client.add_bill(
        book_id=book_id, bill_type=bill_type,
        title=str(args["title"]), trade_time=str(args["trade_time"]),
        account_id=account_id,
        category_amount_pair=pairs, amount=amount,
        to_account_id=to_account_id,
        tag_id_list=tag_ids, payee_id=payee_id,
        order_id=_as_id(args.get("order_id")),
        confirm=bool(args.get("confirm", True)),
        include=bool(args.get("include", True)),
    )
    return {"result": result.get("msg", "ok"), "billType": bill_type}


# ---------------------------------------------------------------------------
# 工具清单（JSON Schema）+ 行为注解
# ---------------------------------------------------------------------------
_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
         "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
          "openWorldHint": False}

_GROUP_PROP = {"group_id": {"type": ["integer", "string"],
                            "description": "分组 ID，省略则使用当前账号的默认分组"}}
_BOOK_PROP = {"book_id": {"type": ["integer", "string"],
                          "description": "账本 ID 或账本名，省略则使用该分组下第一个启用账本"}}
_BILL_TYPE_PROP = {"bill_type": {"type": "integer", "enum": [1, 2, 3],
                                 "description": "账单类型：1=支出，2=收入，3=转账", "default": 1}}


def _props(*dicts):
    out = {}
    for d in dicts:
        out.update(d)
    return out


TOOLS = [
    {
        "name": "fortune_context",
        "title": "获取记账上下文",
        "description": ("获取当前账号的默认分组、默认账本，以及全部账户的名称、ID 与"
                        "当前余额。开始任何记账或查询前的第一步，用于确定 groupId / "
                        "bookId / accountId。"),
        "inputSchema": {"type": "object", "properties": _props(_GROUP_PROP, _BOOK_PROP)},
        "annotations": {"title": "获取记账上下文", **_READ},
    },
    {
        "name": "fortune_list_accounts",
        "title": "查询账户与余额",
        "description": ("列出全部账户及其当前余额、币种和收支权限，并返回账户余额合计。"
                        "用户问“卡里还有多少钱”“查余额”“有哪些账户”时调用。"),
        "inputSchema": {"type": "object", "properties": _props(_GROUP_PROP)},
        "annotations": {"title": "查询账户与余额", **_READ},
    },
    {
        "name": "fortune_list_groups",
        "title": "查询记账分组",
        "description": "列出当前账号可用的记账分组（多账套/家庭共享场景使用）。",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "查询记账分组", **_READ},
    },
    {
        "name": "fortune_list_books",
        "title": "查询账本",
        "description": "列出指定分组下的启用账本及其默认币种、默认账户设置。",
        "inputSchema": {"type": "object", "properties": _props(_GROUP_PROP)},
        "annotations": {"title": "查询账本", **_READ},
    },
    {
        "name": "fortune_list_categories",
        "title": "查询分类",
        "description": ("列出指定账单类型的分类树（含子分类与 categoryId）。记支出前用 "
                        "bill_type=1、记收入前用 bill_type=2 确认分类；用户只说分类名"
                        "（如“餐饮”“工资”）时可先查这里再传名称。"),
        "inputSchema": {"type": "object",
                        "properties": _props(_BILL_TYPE_PROP, _GROUP_PROP, _BOOK_PROP)},
        "annotations": {"title": "查询分类", **_READ},
    },
    {
        "name": "fortune_list_payees",
        "title": "查询收付款方",
        "description": "列出收付款方（如京东商城、美团外卖）及其 payeeId，可在记账时关联。",
        "inputSchema": {"type": "object",
                        "properties": _props(_BILL_TYPE_PROP, _GROUP_PROP, _BOOK_PROP)},
        "annotations": {"title": "查询收付款方", **_READ},
    },
    {
        "name": "fortune_list_tags",
        "title": "查询标签",
        "description": "列出标签树（如早餐、房租、打车）及其 tagId，可在记账时打标签。",
        "inputSchema": {"type": "object",
                        "properties": _props(_BILL_TYPE_PROP, _GROUP_PROP, _BOOK_PROP)},
        "annotations": {"title": "查询标签", **_READ},
    },
    {
        "name": "fortune_add_expense",
        "title": "记一笔支出",
        "description": ("新增一笔支出账单，成功后账户余额立即扣减。用户说“花了 25 块吃"
                        "午饭”“记一笔支出”时调用。account_id / category_id 支持传数字 ID "
                        "或中文名称，名称会模糊匹配；trade_time 省略取当前时间。"),
        "inputSchema": {"type": "object", "properties": _props({
            "title": {"type": "string", "description": "账单标题/说明，如“公司楼下午餐”，必填"},
            "amount": {"type": "number", "description": "支出金额，必须大于 0，单位与账本币种一致", "exclusiveMinimum": 0},
            "account_id": {"type": ["integer", "string"], "description": "付款账户 ID 或账户名，必填"},
            "category_id": {"type": ["integer", "string"], "description": "支出分类 ID 或分类名（如“消费类/餐饮”），必填"},
            "trade_time": {"type": "string", "description": "交易时间，格式 YYYY-MM-DD HH:MM:SS，省略取当前时间"},
            "tag_ids": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "标签 ID 列表，可选"},
            "payee_id": {"type": ["integer", "string"], "description": "收付款方 ID，可选"},
        }, _GROUP_PROP, _BOOK_PROP),
            "required": ["title", "amount", "account_id", "category_id"]},
        "annotations": {"title": "记一笔支出", **_WRITE},
    },
    {
        "name": "fortune_add_income",
        "title": "记一笔收入",
        "description": ("新增一笔收入账单，成功后账户余额立即增加。用户说“发工资 8000”"
                        "“记一笔收入”时调用。account_id / category_id 支持数字 ID 或中文名称。"),
        "inputSchema": {"type": "object", "properties": _props({
            "title": {"type": "string", "description": "账单标题/说明，如“10 月工资”，必填"},
            "amount": {"type": "number", "description": "收入金额，必须大于 0", "exclusiveMinimum": 0},
            "account_id": {"type": ["integer", "string"], "description": "收款账户 ID 或账户名，必填"},
            "category_id": {"type": ["integer", "string"], "description": "收入分类 ID 或分类名（如“工资”），必填"},
            "trade_time": {"type": "string", "description": "交易时间，格式 YYYY-MM-DD HH:MM:SS，省略取当前时间"},
            "tag_ids": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "标签 ID 列表，可选"},
            "payee_id": {"type": ["integer", "string"], "description": "收付款方 ID，可选"},
        }, _GROUP_PROP, _BOOK_PROP),
            "required": ["title", "amount", "account_id", "category_id"]},
        "annotations": {"title": "记一笔收入", **_WRITE},
    },
    {
        "name": "fortune_add_transfer",
        "title": "记一笔转账",
        "description": ("在账户之间转账（如“从账户1 转 100 到账户2”），转出账户余额减少、"
                        "转入账户余额增加。转出与转入账户不能相同。"),
        "inputSchema": {"type": "object", "properties": _props({
            "title": {"type": "string", "description": "转账说明，必填"},
            "amount": {"type": "number", "description": "转账金额，必须大于 0", "exclusiveMinimum": 0},
            "account_id": {"type": ["integer", "string"], "description": "转出账户 ID 或账户名，必填"},
            "to_account_id": {"type": ["integer", "string"], "description": "转入账户 ID 或账户名，必填"},
            "trade_time": {"type": "string", "description": "交易时间，格式 YYYY-MM-DD HH:MM:SS，省略取当前时间"},
        }, _GROUP_PROP, _BOOK_PROP),
            "required": ["title", "amount", "account_id", "to_account_id"]},
        "annotations": {"title": "记一笔转账", **_WRITE},
    },
    {
        "name": "fortune_add_account",
        "title": "新增账户",
        "description": ("新建一个账户（银行卡/现金/支付宝/微信等），可带初始余额。"
                        "用户说“加一个现金账户，里面有 200 块”时调用。"),
        "inputSchema": {"type": "object", "properties": _props({
            "account_name": {"type": "string", "description": "账户名称，如“招商银行卡”，必填"},
            "balance": {"type": "number", "description": "初始余额，默认 0"},
            "currency_code": {"type": "string", "description": "币种代码，默认 CNY"},
            "card_no": {"type": "string", "description": "卡号后四位或备注编号，可选"},
            "account_type": {"type": "integer", "description": "账户类型，默认 1"},
        }, _GROUP_PROP), "required": ["account_name"]},
        "annotations": {"title": "新增账户", **_WRITE},
    },
    {
        "name": "fortune_add_bill",
        "title": "高级记账（原始字段）",
        "description": ("高级通用记账接口，直接提交原始账单字段，支持一笔多分类分摊、"
                        "关联单据 orderId、显式指定 confirm/include。仅在 "
                        "fortune_add_expense / fortune_add_income / fortune_add_transfer "
                        "无法满足需求时使用。"),
        "inputSchema": {"type": "object", "properties": {
            "group_id": {"type": ["integer", "string"], "description": "分组 ID，省略则用默认分组"},
            "book_id": {"type": "integer", "description": "账本 ID，必填"},
            "bill_type": {"type": "integer", "enum": [1, 2, 3], "description": "1=支出 2=收入 3=转账，必填"},
            "title": {"type": "string", "description": "标题，必填"},
            "trade_time": {"type": "string", "description": "格式 YYYY-MM-DD HH:MM:SS，必填"},
            "account_id": {"type": "integer", "description": "账户 ID（转账时为转出账户），必填"},
            "category_amount_pair": {"type": "array", "items": {"type": "object"},
                                     "description": "分类金额对，如 [{\"categoryId\":114,\"amount\":10}]；支出/收入必填"},
            "amount": {"type": "number", "description": "转账金额（仅 bill_type=3 时使用）"},
            "to_account_id": {"type": "integer", "description": "转入账户 ID（仅转账）"},
            "tag_id_list": {"type": "array", "items": {"type": "integer"}, "description": "标签 ID 列表"},
            "payee_id": {"type": "integer", "description": "收付款方 ID"},
            "order_id": {"type": "integer", "description": "关联单据 ID"},
            "confirm": {"type": "boolean", "description": "是否确认，默认 true"},
            "include": {"type": "boolean", "description": "是否计入统计，默认 true"},
        }, "required": ["book_id", "bill_type", "title", "trade_time", "account_id"]},
        "annotations": {"title": "高级记账（原始字段）", **_WRITE},
    },
    {
        "name": "fortune_login",
        "title": "登录记账系统",
        "description": ("手动登录并刷新访问令牌。正常情况下其他工具会自动登录，"
                        "仅在令牌失效或需要切换状态时调用。"),
        "inputSchema": {"type": "object", "properties": {
            "force": {"type": "boolean", "description": "是否强制重新登录", "default": False},
        }},
        "annotations": {"title": "登录记账系统",
                        "readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
]

TOOL_HANDLERS = {
    "fortune_login": tool_login,
    "fortune_context": tool_context,
    "fortune_list_groups": tool_list_groups,
    "fortune_list_books": tool_list_books,
    "fortune_list_accounts": tool_list_accounts,
    "fortune_add_account": tool_add_account,
    "fortune_list_categories": tool_list_categories,
    "fortune_list_payees": tool_list_payees,
    "fortune_list_tags": tool_list_tags,
    "fortune_add_expense": tool_add_expense,
    "fortune_add_income": tool_add_income,
    "fortune_add_transfer": tool_add_transfer,
    "fortune_add_bill": tool_add_bill,
}


def call_tool(client, name, arguments):
    """执行工具，返回 (result_dict, is_error)。"""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise FortuneError(f"未知工具: {name}")
    return handler(client, arguments or {}), False
