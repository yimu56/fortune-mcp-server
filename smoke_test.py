#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fortune MCP Server 冒烟测试（纯标准库，可在服务器上直接跑）。

用法：
    python3 smoke_test.py                                  # 默认 http://127.0.0.1:18088/mcp
    python3 smoke_test.py http://127.0.0.1:18088/mcp TOKEN  # 带 Bearer Token
    python3 smoke_test.py <url> <token> --write            # 追加写入测试（转账 0.01 并转回）

退出码 0 表示全部通过。
"""

import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 30


class McpClient:
    def __init__(self, url, token=None):
        self.url = url
        self.token = token
        self.sid = None
        self.protocol = ""
        self.rid = 0

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def rpc(self, method, params=None, notify=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self.rid += 1
            payload["id"] = self.rid
        req = urllib.request.Request(self.url, method="POST",
                                     data=json.dumps(payload).encode(),
                                     headers=self._headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.sid = sid
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status == 202 or not body.strip():
                return None
            return json.loads(body)

    def tool(self, name, args):
        resp = self.rpc("tools/call", {"name": name, "arguments": args})
        result = resp.get("result") or {}
        texts = [c.get("text") for c in result.get("content", [])
                 if c.get("type") == "text"]
        if result.get("isError"):
            raise RuntimeError("工具 %s 返回错误: %s" % (name, " ".join(texts)))
        return json.loads(texts[0]) if texts else result


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = argv[0] if argv else "http://127.0.0.1:18088/mcp"
    token = argv[1] if len(argv) > 1 else None
    do_write = "--write" in sys.argv

    client = McpClient(url, token)
    steps = []

    init = client.rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "fortune-smoke-test", "version": "1.0"},
    })
    assert init and init["result"]["serverInfo"], "initialize 失败"
    info = init["result"]
    client.protocol = info["protocolVersion"]
    steps.append("initialize  ok  protocol=%s session=%s"
                 % (info["protocolVersion"], client.sid))

    client.rpc("notifications/initialized", notify=True)
    steps.append("initialized  ok")

    tools = client.rpc("tools/list")["result"]["tools"]
    steps.append("tools/list  ok  共 %d 个工具" % len(tools))
    names = [t["name"] for t in tools]
    assert "fortune_add_expense" in names, "缺少记账工具"

    ctx = client.tool("fortune_context", {})
    accounts = ctx["accounts"]
    steps.append("fortune_context  ok  group=%s book=%s 账户=%d"
                 % (ctx["groupId"], ctx["bookId"],
                    len(accounts)))
    assert accounts, "没有可用账户"

    acc = client.tool("fortune_list_accounts", {})
    steps.append("fortune_list_accounts  ok  合计余额=%s" % acc["totalBalance"])

    cats = client.tool("fortune_list_categories", {"bill_type": 1})
    steps.append("fortune_list_categories  ok  支出分类=%d"
                 % len(cats["categories"]))

    if do_write and len(accounts) >= 2:
        a, b = accounts[0], accounts[1]
        before = (a["balance"], b["balance"])
        client.tool("fortune_add_transfer", {
            "title": "MCP 连通性测试-转出", "amount": 0.01,
            "account_id": a["accountId"], "to_account_id": b["accountId"]})
        client.tool("fortune_add_transfer", {
            "title": "MCP 连通性测试-转回", "amount": 0.01,
            "account_id": b["accountId"], "to_account_id": a["accountId"]})
        after_ctx = client.tool("fortune_context", {})
        after = {x["accountId"]: x["balance"] for x in after_ctx["accounts"]}
        assert abs(after[a["accountId"]] - before[0]) < 0.005, "转回后余额未复原"
        steps.append("fortune_add_transfer  ok  余额已复原 (%s / %s)"
                     % (after[a["accountId"]], after[b["accountId"]]))

    # 错误处理：金额非法应返回 isError 而不是崩连接
    resp = client.rpc("tools/call", {"name": "fortune_add_expense", "arguments": {
        "title": "x", "amount": 0, "account_id": accounts[0]["accountId"],
        "category_id": 114}})
    assert isinstance(resp.get("result", {}).get("isError"), bool)
    steps.append("参数校验  ok  非法金额被拦截")

    print("\n".join("  · " + s for s in steps))
    print("\n全部通过 ✔  (%s)" % url)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("测试失败: %r" % exc)
        sys.exit(1)
