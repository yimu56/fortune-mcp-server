#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fortune 记账远程 MCP Server（纯 Python 标准库，零第三方依赖）。

三种用法：
  1) 远程服务（推荐给企业版连接器）
         python3 server.py
         # Streamable HTTP : POST    http://<host>:18088/mcp
         # 旧版 SSE        : GET     http://<host>:18088/sse  + POST /messages
         # 健康检查        : GET     http://<host>:18088/healthz
  2) 本地 stdio（桌面客户端 mcp.json）
         python3 server.py --stdio
  3) 生产守护
         systemd + 环境变量文件（见 install.sh / deploy/）

环境变量见 .env.example。所有日志写 stderr，stdout 在 stdio 模式下只输出协议消息。
"""

import argparse
import hmac
import json
import os
import queue
import signal
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import fortune_tools
from fortune_api import FortuneClient, FortuneError

SERVER_NAME = fortune_tools.SERVER_NAME
SERVER_VERSION = fortune_tools.SERVER_VERSION

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
LATEST_PROTOCOL = "2025-06-18"


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        env = os.environ.get
        self.host = env("MCP_HOST") or "0.0.0.0"
        self.port = int(env("MCP_PORT") or 18088)
        self.path = env("MCP_PATH") or "/mcp"
        if not self.path.startswith("/"):
            self.path = "/" + self.path
        self.auth_token = (env("MCP_AUTH_TOKEN") or "").strip()
        self.tls_cert = env("MCP_TLS_CERT") or ""
        self.tls_key = env("MCP_TLS_KEY") or ""
        self.session_ttl = int(env("MCP_SESSION_TTL") or 3600)
        self.debug = env("MCP_DEBUG") == "1"
        self.allow_header_cred = env("FORTUNE_ALLOW_HEADER_CRED") == "1"
        self.base_url = (env("FORTUNE_BASE_URL")
                         or "http://fortune.example.com:46031/prod-api").rstrip("/")
        self.username = env("FORTUNE_USERNAME") or ""
        self.password = env("FORTUNE_PASSWORD") or ""
        self.encrypted_password = env("FORTUNE_ENCRYPTED_PASSWORD") or ""
        self.token = env("FORTUNE_TOKEN") or ""

    def default_creds(self):
        return {"base_url": self.base_url, "username": self.username,
                "password": self.password,
                "encrypted_password": self.encrypted_password,
                "token": self.token}

    def summary(self):
        return {"baseUrl": self.base_url,
                "username": self.username or "(未配置)",
                "authRequired": bool(self.auth_token),
                "headerCred": self.allow_header_cred}


# ---------------------------------------------------------------------------
# 客户端池：按凭据复用，避免每次调用都重新登录
# ---------------------------------------------------------------------------
class ClientPool:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._clients = {}

    def get(self, creds):
        key = (creds["base_url"], creds["username"],
               bool(creds["password"]), (creds["token"] or "")[:20])
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = FortuneClient(
                    base_url=creds["base_url"], username=creds["username"],
                    password=creds["password"],
                    encrypted_password=creds["encrypted_password"],
                    token=creds["token"])
                self._clients[key] = client
        client.ensure_login()
        return client


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, sid, creds):
        self.sid = sid
        self.creds = creds
        self.protocol = LATEST_PROTOCOL
        self.created = time.time()
        self.touched = time.time()
        self.queue = queue.Queue()
        self.closed = False


class SessionStore:
    def __init__(self, ttl):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._sessions = {}

    def create(self, creds):
        sid = uuid.uuid4().hex
        sess = Session(sid, creds)
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            sess = self._sessions.get(sid)
        if sess and time.time() - sess.touched > self.ttl:
            self.drop(sid)
            return None
        if sess:
            sess.touched = time.time()
        return sess

    def drop(self, sid):
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess:
            sess.closed = True
            sess.queue.put(None)
        return sess

    def sweep(self):
        now = time.time()
        with self._lock:
            dead = [s for s, v in self._sessions.items()
                    if now - v.touched > self.ttl]
        for sid in dead:
            self.drop(sid)
        return len(dead)


# ---------------------------------------------------------------------------
# 协议引擎（HTTP 与 stdio 共用）
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self, cfg, pool):
        self.cfg = cfg
        self.pool = pool

    # ---- 消息分发 ---------------------------------------------------------
    def handle(self, msg, session):
        """处理一条 JSON-RPC 消息，返回响应 dict；通知类消息返回 None。"""
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if msg_id is None and method:
            return None  # notifications/* 等通知，按规范不响应

        if method == "initialize":
            requested = params.get("protocolVersion") or LATEST_PROTOCOL
            session.protocol = (requested if requested in SUPPORTED_PROTOCOLS
                                else LATEST_PROTOCOL)
            return self._ok(msg_id, {
                "protocolVersion": session.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                               "title": "Fortune 记账"},
                "instructions": fortune_tools.INSTRUCTIONS,
            })

        if method == "ping":
            return self._ok(msg_id, {})

        if method == "tools/list":
            return self._ok(msg_id, {"tools": fortune_tools.TOOLS})

        if method == "tools/call":
            return self._call_tool(msg_id, params, session)

        if method in ("resources/list",):
            return self._ok(msg_id, {"resources": []})
        if method in ("resources/templates/list",):
            return self._ok(msg_id, {"resourceTemplates": []})
        if method in ("prompts/list",):
            return self._ok(msg_id, {"prompts": []})
        if method in ("logging/setLevel", "completion/complete"):
            return self._ok(msg_id, {})

        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "未知方法: %s" % method}}

    def _ok(self, msg_id, result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call_tool(self, msg_id, params, session):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            client = self.pool.get(session.creds)
            payload, _ = fortune_tools.call_tool(client, name, arguments)
        except FortuneError as exc:
            text = "调用失败：%s" % exc
            if exc.response:
                text += "\n" + json.dumps(exc.response, ensure_ascii=False)[:600]
            log("工具 %s 失败: %s" % (name, exc))
            return self._ok(msg_id, self._error_result(text))
        except Exception as exc:  # noqa: BLE001
            log("工具 %s 异常: %r" % (name, exc))
            if self.cfg.debug:
                import traceback
                traceback.print_exc(file=sys.stderr)
            return self._ok(msg_id, self._error_result("服务端异常：%s" % exc))

        result = {"content": [{"type": "text",
                               "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                  "isError": False}
        if session.protocol >= "2025-06-18":
            result["structuredContent"] = payload
        return self._ok(msg_id, result)

    @staticmethod
    def _error_result(text):
        return {"content": [{"type": "text", "text": text}], "isError": True}


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FortuneMCP/" + SERVER_VERSION

    # ---- 基础工具 ---------------------------------------------------------
    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, Mcp-Session-Id, "
                         "MCP-Protocol-Version, Last-Event-ID, X-MCP-Token, "
                         "X-Fortune-Username, X-Fortune-Password, "
                         "X-Fortune-Base-Url")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status, obj, extra=None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length:
            raw = self.rfile.read(int(length))
        elif (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            raw = b"".join(chunks)
        else:
            raw = b""
        return raw.decode("utf-8", errors="replace")

    # ---- 鉴权 -------------------------------------------------------------
    def _authorized(self):
        token = self.app.cfg.auth_token
        if not token:
            return True
        given = ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not given:
            given = (self.headers.get("X-MCP-Token") or "").strip()
        if not given:
            qs = parse_qs(urlparse(self.path).query)
            given = (qs.get("token") or [""])[0]
        return bool(given) and hmac.compare_digest(given, token)

    def _deny(self):
        self._send_json(401, {"error": "unauthorized",
                              "message": "缺少或错误的 Bearer Token"})

    def _creds(self):
        cfg = self.app.cfg
        creds = cfg.default_creds()
        if cfg.allow_header_cred:
            user = (self.headers.get("X-Fortune-Username") or "").strip()
            pwd = self.headers.get("X-Fortune-Password") or ""
            base = (self.headers.get("X-Fortune-Base-Url") or "").strip()
            if user:
                creds["username"] = user
                creds["password"] = pwd
                creds["token"] = ""
            if base:
                creds["base_url"] = base.rstrip("/")
        return creds

    def _session(self, create_if_missing=True):
        sid = self.headers.get("Mcp-Session-Id")
        sess = self.app.sessions.get(sid)
        if sess is None and create_if_missing:
            sess = self.app.sessions.create(self._creds())
        if sess:
            # 无状态客户端靠 MCP-Protocol-Version 头声明协议版本
            version = self.headers.get("MCP-Protocol-Version")
            if version in SUPPORTED_PROTOCOLS:
                sess.protocol = version
            sess.touched = time.time()
        return sess

    # ---- 路由 -------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/healthz":
            self._send_json(200, {
                "status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION,
                "protocols": list(SUPPORTED_PROTOCOLS),
                "tools": len(fortune_tools.TOOLS),
                "sessions": len(getattr(self.server.app, "sessions")._sessions),
                "config": self.app.cfg.summary(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return

        if path == "/":
            self._send_json(200, {
                "service": SERVER_NAME, "version": SERVER_VERSION,
                "transport": {"streamableHttp": "POST " + self.app.cfg.path,
                              "legacySse": "GET /sse"},
                "health": "/healthz", "tools": len(fortune_tools.TOOLS),
            })
            return

        if not self._authorized():
            self._deny()
            return

        if path == "/sse":
            self._handle_sse()
            return

        if path == self.app.cfg.path.rstrip("/"):
            self._send_json(405, {"error": "method_not_allowed",
                                  "message": "请使用 POST 发送 JSON-RPC 消息"})
            return

        self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._authorized():
            self._deny()
            return
        if path == "/messages":
            self._handle_legacy_message(parse_qs(parsed.query))
            return
        if path == self.app.cfg.path.rstrip("/"):
            self._handle_streamable()
            return
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not_found", "path": path})

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == self.app.cfg.path.rstrip("/"):
            sid = self.headers.get("Mcp-Session-Id")
            if sid:
                self.app.sessions.drop(sid)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return
        self._send_json(404, {"error": "not_found"})

    # ---- Streamable HTTP --------------------------------------------------
    def _handle_streamable(self):
        raw = self._read_body()
        try:
            payload = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as exc:
            self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700,
                                            "message": "JSON 解析失败: %s" % exc}})
            return
        if payload is None:
            self._send_json(400, {"error": "empty_body"})
            return

        session = self._session()
        is_init = isinstance(payload, dict) and payload.get("method") == "initialize"
        headers = {"Mcp-Session-Id": session.sid}

        if isinstance(payload, list):
            responses = [r for r in (self.app.engine.handle(m, session) for m in payload)
                         if r is not None]
            self._send_json(200, responses or None, headers)
            return

        response = self.app.engine.handle(payload, session)
        if response is None:
            # 通知：202 无响应体
            self.send_response(202)
            self.send_header("Content-Length", "0")
            for k, v in headers.items():
                self.send_header(k, v)
            self._cors()
            self.end_headers()
            return
        if is_init:
            headers["MCP-Protocol-Version"] = session.protocol
        self._send_json(200, response, headers)

    # ---- 旧版 HTTP+SSE ----------------------------------------------------
    def _handle_sse(self):
        session = self._session()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self._cors()
        self.end_headers()

        def chunk(text):
            data = text.encode("utf-8")
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

        try:
            chunk("event: endpoint\ndata: /messages?sessionId=%s\n\n" % session.sid)
            while not session.closed and not self.server.stopping:
                try:
                    item = session.queue.get(timeout=15)
                except queue.Empty:
                    chunk(": keepalive\n\n")
                    continue
                if item is None:
                    break
                chunk("event: message\ndata: %s\n\n"
                      % json.dumps(item, ensure_ascii=False))
            chunk("")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            log("SSE 会话结束: %s" % session.sid)
            self.app.sessions.drop(session.sid)
            self.close_connection = True

    def _handle_legacy_message(self, qs):
        sid = (qs.get("sessionId") or [""])[0]
        session = self.app.sessions.get(sid)
        if session is None:
            self._send_json(404, {"error": "session_not_found", "sessionId": sid})
            return
        raw = self._read_body()
        try:
            payload = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": "bad_json", "message": str(exc)})
            return
        messages = payload if isinstance(payload, list) else [payload]
        for msg in messages:
            response = self.app.engine.handle(msg, session)
            if response is not None:
                session.queue.put(response)
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()


# ---------------------------------------------------------------------------
# stdio 模式
# ---------------------------------------------------------------------------
def run_stdio(cfg, pool):
    engine = Engine(cfg, pool)
    store = SessionStore(cfg.session_ttl)
    session = store.create(cfg.default_creds())
    log("stdio 模式启动，工具数: %d，目标: %s" % (len(fortune_tools.TOOLS), cfg.base_url))

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            log("非法 JSON: %s" % exc)
            continue
        try:
            for m in (msg if isinstance(msg, list) else [msg]):
                resp = engine.handle(m, session)
                if resp is not None:
                    send(resp)
        except Exception as exc:  # noqa: BLE001
            log("消息处理异常: %r" % exc)


# ---------------------------------------------------------------------------
# 远程服务模式
# ---------------------------------------------------------------------------
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pool = ClientPool(cfg)
        self.engine = Engine(cfg, self.pool)
        self.sessions = SessionStore(cfg.session_ttl)


def sweep_loop(app, httpd):
    while not httpd.stopping:
        time.sleep(60)
        try:
            dropped = app.sessions.sweep()
            if dropped:
                log("清理过期会话 %d 个" % dropped)
        except Exception:
            pass


def run_http(cfg, app):
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    httpd.stopping = False

    scheme = "http"
    if cfg.tls_cert and cfg.tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"

    def shutdown(signum, frame):
        log("收到信号 %s，准备停止" % signum)
        httpd.stopping = True
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    except ValueError:
        pass

    threading.Thread(target=sweep_loop, args=(app, httpd), daemon=True).start()

    log("Fortune MCP Server %s 已启动" % SERVER_VERSION)
    log("  监听        : %s://%s:%d" % (scheme, cfg.host, cfg.port))
    log("  Streamable  : POST %s://<host>:%d%s"
        % (scheme, cfg.port, cfg.path))
    log("  旧版 SSE    : GET  %s://<host>:%d/sse" % (scheme, cfg.port))
    log("  健康检查    : GET  %s://<host>:%d/healthz" % (scheme, cfg.port))
    log("  记账目标    : %s（账号 %s）"
        % (cfg.base_url, cfg.username or "未配置"))
    log("  工具数      : %d" % len(fortune_tools.TOOLS))
    if not cfg.auth_token:
        log("  ⚠ 未设置 MCP_AUTH_TOKEN，任何人都能调用这些记账工具，"
            "上线前请在 .env 中设置强口令")
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.stopping = True
        httpd.server_close()
        log("已停止")


def main():
    parser = argparse.ArgumentParser(description="Fortune 记账 MCP Server")
    parser.add_argument("--stdio", action="store_true", help="以 stdio 模式运行（本地客户端）")
    parser.add_argument("--port", type=int, help="覆盖 MCP_PORT")
    parser.add_argument("--host", help="覆盖 MCP_HOST")
    parser.add_argument("--self-check", action="store_true",
                        help="检查配置与记账服务连通性后退出")
    args = parser.parse_args()

    cfg = Config()
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host

    if args.self_check:
        pool = ClientPool(cfg)
        print(json.dumps({"config": cfg.summary()}, ensure_ascii=False, indent=2))
        try:
            client = pool.get(cfg.default_creds())
            gid = client.get_default_group_id()
            print(json.dumps({"login": "ok", "defaultGroupId": gid,
                              "baseUrl": cfg.base_url}, ensure_ascii=False))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"login": "fail", "error": str(exc)},
                             ensure_ascii=False))
            return 1

    if args.stdio:
        run_stdio(cfg, ClientPool(cfg))
        return 0

    run_http(cfg, App(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
