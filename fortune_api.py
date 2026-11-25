#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fortune 记账系统 HTTP 客户端（纯 Python 标准库，零第三方依赖）。

环境变量:
  FORTUNE_BASE_URL            接口根地址，默认 http://fortune.example.com:46031/prod-api
  FORTUNE_USERNAME            登录账号
  FORTUNE_PASSWORD            明文密码（优先，登录时自动 RSA 加密）
  FORTUNE_ENCRYPTED_PASSWORD  已加密密码（回放抓包密文，密码为空时使用）
  FORTUNE_TOKEN               直接指定 JWT，跳过登录
  FORTUNE_TOKEN_CACHE         设为 0 关闭本地 token 缓存，默认开启
  FORTUNE_TOKEN_CACHE_DIR     token 缓存目录，默认 ~/.cache/fortune-mcp
  FORTUNE_HTTP_TIMEOUT        单次请求超时秒数，默认 20
"""

import base64
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://fortune.example.com:46031/prod-api"
HOME = os.path.expanduser("~")
_CACHE_DIR = os.environ.get("FORTUNE_TOKEN_CACHE_DIR") or os.path.join(
    HOME, ".cache", "fortune-mcp")
TOKEN_CACHE_PATH = os.path.join(_CACHE_DIR, "fortune_token_cache.json")

_cache_lock = threading.RLock()


class FortuneError(Exception):
    """接口调用失败。"""

    def __init__(self, message, code=None, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


# ---------------------------------------------------------------------------
# 纯手写 DER 解析 + RSA PKCS#1 v1.5 加密（与前端 JSEncrypt 行为一致）
# ---------------------------------------------------------------------------
def _read_tlv(data, pos):
    if pos + 2 > len(data):
        raise ValueError("DER 数据不完整")
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or pos + n > len(data):
            raise ValueError("DER 长度字段非法")
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    if pos + length > len(data):
        raise ValueError("DER 内容越界")
    return tag, data[pos:pos + length], pos + length


def _parse_rsa_public_key(public_key_b64):
    """解析 X.509 SubjectPublicKeyInfo (base64 DER)，返回 (n, e, k)。"""
    der = base64.b64decode(public_key_b64)
    _, spki, _ = _read_tlv(der, 0)
    pos = 0
    _, _alg, pos = _read_tlv(spki, pos)          # AlgorithmIdentifier
    _, bit_string, pos = _read_tlv(spki, pos)    # BIT STRING
    if not bit_string or bit_string[0] != 0:
        raise ValueError("BIT STRING 格式异常")
    rsa_pub = bit_string[1:]
    _, seq, _ = _read_tlv(rsa_pub, 0)
    pos = 0
    _, n_bytes, pos = _read_tlv(seq, pos)
    _, e_bytes, pos = _read_tlv(seq, pos)
    n = int.from_bytes(n_bytes, "big")
    e = int.from_bytes(e_bytes, "big")
    return n, e, (n.bit_length() + 7) // 8


def rsa_encrypt(text, public_key_b64):
    """RSA/ECB/PKCS1Padding 加密并以 base64 返回。"""
    n, e, k = _parse_rsa_public_key(public_key_b64)
    message = text.encode("utf-8")
    if len(message) > k - 11:
        raise ValueError("明文过长，超出 RSA 单次加密上限")
    padding_len = k - len(message) - 3
    padding = bytearray()
    while len(padding) < padding_len:
        padding.append(secrets.randbelow(255) + 1)  # 非零随机字节
    encoded = b"\x00\x02" + bytes(padding) + b"\x00" + message
    cipher_int = pow(int.from_bytes(encoded, "big"), e, n)
    return base64.b64encode(cipher_int.to_bytes(k, "big")).decode("ascii")


# ---------------------------------------------------------------------------
# Token 缓存（进程内加锁 + 原子写入，避免多线程重复登录 / 写坏文件）
# ---------------------------------------------------------------------------
def _load_token_cache():
    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_token_cache(cache):
    with _cache_lock:
        try:
            os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
            tmp = TOKEN_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, TOKEN_CACHE_PATH)
            try:
                os.chmod(TOKEN_CACHE_PATH, 0o600)
            except Exception:
                pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class FortuneClient:
    BILL_EXPENSE = 1
    BILL_INCOME = 2
    BILL_TRANSFER = 3

    def __init__(self, base_url=None, username=None, password=None,
                 encrypted_password=None, token=None, timeout=None):
        self.base_url = (base_url or os.environ.get("FORTUNE_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.username = username or os.environ.get("FORTUNE_USERNAME") or ""
        self.password = password or os.environ.get("FORTUNE_PASSWORD") or ""
        self.encrypted_password = (encrypted_password
                                   or os.environ.get("FORTUNE_ENCRYPTED_PASSWORD") or "")
        self.token = token or os.environ.get("FORTUNE_TOKEN") or ""
        self.timeout = timeout or float(os.environ.get("FORTUNE_HTTP_TIMEOUT") or 20)
        self.user_info = None
        self._login_lock = threading.RLock()
        self._cache_enabled = os.environ.get("FORTUNE_TOKEN_CACHE", "1") != "0"
        self._cache_key = f"{self.base_url}|{self.username}"
        if not self.token and self._cache_enabled:
            with _cache_lock:
                cached = _load_token_cache().get(self._cache_key) or {}
            if cached.get("exp", 0) > time.time() + 60:
                self.token = cached.get("token", "")

    # ---- 底层请求 ----------------------------------------------------------
    def _request(self, method, path, json_body=None, data_json=None,
                 query=None, auth=True, _retry=True):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 FortuneBookkeepingMCP/2.0",
        }
        body = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        elif data_json is not None:
            boundary = "----FortuneMCPBoundary" + secrets.token_hex(12)
            part = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="data"; filename="blob"\r\n'
                "Content-Type: application/json\r\n\r\n"
                + json.dumps(data_json, ensure_ascii=False) + "\r\n"
                + f"--{boundary}--\r\n"
            ).encode("utf-8")
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            body = part
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403) and auth and _retry:
                # token 失效：强制重登一次再重试
                self.token = ""
                self.login(force=True)
                return self._request(method, path, json_body=json_body,
                                     data_json=data_json, query=query,
                                     auth=auth, _retry=False)
            raise FortuneError(f"HTTP {exc.code}: {raw[:300]}", code=exc.code)
        except urllib.error.URLError as exc:
            raise FortuneError(f"无法连接记账服务 {self.base_url}：{exc.reason}")
        except TimeoutError:
            raise FortuneError(f"请求超时（{self.timeout}s）：{url}")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise FortuneError(f"响应不是 JSON: {raw[:300]}")
        if isinstance(result, dict) and result.get("code") not in (0, 200, None):
            raise FortuneError(
                f"接口错误 code={result.get('code')} msg={result.get('msg')}",
                code=result.get("code"), response=result,
            )
        return result

    # ---- 鉴权 -------------------------------------------------------------
    def get_rsa_public_key(self):
        return self._request("GET", "/getRsaPublicKey", auth=False)["data"]

    def login(self, username=None, password=None, encrypted_password=None,
              force=False):
        """登录并缓存 token。优先级：明文密码 > 已加密密码。"""
        with self._login_lock:
            if self.token and not force:
                return self.token
            username = username or self.username
            plaintext = password or self.password
            enc = encrypted_password or self.encrypted_password
            if not username:
                raise FortuneError("缺少账号：请设置 FORTUNE_USERNAME")
            if plaintext:
                enc = rsa_encrypt(plaintext, self.get_rsa_public_key())
            elif not enc:
                raise FortuneError(
                    "缺少密码：请设置 FORTUNE_PASSWORD（明文）或 "
                    "FORTUNE_ENCRYPTED_PASSWORD（抓包密文）")
            result = self._request("POST", "/login", json_body={
                "username": username,
                "password": enc,
                "captchaCode": "",
                "captchaCodeKey": "",
            }, auth=False)
            data = result.get("data") or {}
            self.token = data.get("token") or ""
            self.user_info = data.get("currentUser") or {}
            if not self.token:
                raise FortuneError("登录响应中未返回 token", response=result)
            if self._cache_enabled:
                with _cache_lock:
                    cache = _load_token_cache()
                cache[self._cache_key] = {"token": self.token,
                                          "exp": time.time() + 29 * 86400}
                _save_token_cache(cache)
            return self.token

    def ensure_login(self):
        if not self.token:
            self.login()
        return self.token

    # ---- 分组 / 账本 -------------------------------------------------------
    def get_default_group_id(self):
        self.ensure_login()
        data = self._request("GET", "/fortune/group/getDefaultGroupId")["data"]
        return int(data) if str(data).isdigit() else data

    def get_groups(self):
        return self._request("GET", "/fortune/group/getEnableList")["data"]

    def get_group(self, group_id):
        return self._request(
            "GET", f"/fortune/group/{group_id}/getByGroupId")["data"]

    def get_books(self, group_id):
        return self._request(
            "GET", f"/fortune/book/base/{group_id}/getEnableList")["data"]

    def get_book(self, book_id):
        return self._request(
            "GET", f"/fortune/book/base/{book_id}/getBookById")["data"]

    # ---- 账户 -------------------------------------------------------------
    def get_accounts(self, group_id):
        return self._request(
            "GET", f"/fortune/account/{group_id}/getEnableList")["data"]

    def get_account_page(self, group_id, account_type=1, recycle_bin=False):
        return self._request("GET", "/fortune/account/getPage", query={
            "groupId": group_id, "recycleBin": str(recycle_bin).lower(),
            "accountType": account_type,
        })["data"]

    def add_account(self, group_id, account_name, balance=0, currency_code="CNY",
                    account_type=1, card_no="", can_expense=True, can_income=True,
                    can_transfer_out=True, can_transfer_in=True, include=True,
                    enable=True):
        payload = {
            "currencyCode": currency_code,
            "accountType": account_type,
            "canExpense": can_expense,
            "canIncome": can_income,
            "canTransferOut": can_transfer_out,
            "canTransferIn": can_transfer_in,
            "include": include,
            "enable": enable,
            "groupId": group_id,
            "accountName": account_name,
            "cardNo": card_no,
            "balance": balance,
        }
        return self._request("POST", "/fortune/account/add", json_body=payload)

    # ---- 分类 / 收付款方 / 标签 ------------------------------------------
    def get_categories(self, book_id, bill_type=1):
        return self._request(
            "GET", f"/fortune/category/{book_id}/getEnableList",
            query={"billType": bill_type})["data"]

    def get_payees(self, book_id, bill_type=1):
        return self._request(
            "GET", f"/fortune/payee/{book_id}/getEnableList",
            query={"billType": bill_type})["data"]

    def get_tags(self, book_id, bill_type=1):
        return self._request(
            "GET", f"/fortune/tag/{book_id}/getEnableList",
            query={"billType": bill_type})["data"]

    def get_finance_orders(self, book_id):
        return self._request(
            "GET", f"/fortune/finance/order/{book_id}/getUsingFinanceOrderList")["data"]

    # ---- 记账 -------------------------------------------------------------
    def add_bill(self, book_id, bill_type, title, trade_time, account_id,
                 category_amount_pair=None, amount=None, to_account_id=None,
                 tag_id_list=None, payee_id=None, order_id=None,
                 confirm=True, include=True):
        """新增一笔账单。bill_type: 1=支出 2=收入 3=转账。"""
        if bill_type not in (1, 2, 3):
            raise FortuneError("bill_type 必须是 1(支出)/2(收入)/3(转账)")
        payload = {
            "billType": bill_type,
            "confirm": confirm,
            "include": include,
            "categoryAmountPair": category_amount_pair or [],
            "bookId": book_id,
            "title": title,
            "tradeTime": trade_time,
            "accountId": account_id,
            "tagIdList": tag_id_list or [],
            "payeeId": payee_id,
            "amount": amount,
            "toAccountId": to_account_id,
            "orderId": order_id,
        }
        return self._request("POST", "/fortune/bill/add", data_json=payload)

    def add_expense(self, book_id, title, amount, trade_time, account_id,
                    category_id, tag_id_list=None, payee_id=None):
        return self.add_bill(book_id, self.BILL_EXPENSE, title, trade_time,
                             account_id,
                             [{"categoryId": category_id, "amount": amount}],
                             tag_id_list=tag_id_list, payee_id=payee_id)

    def add_income(self, book_id, title, amount, trade_time, account_id,
                   category_id, tag_id_list=None, payee_id=None):
        return self.add_bill(book_id, self.BILL_INCOME, title, trade_time,
                             account_id,
                             [{"categoryId": category_id, "amount": amount}],
                             tag_id_list=tag_id_list, payee_id=payee_id)

    def add_transfer(self, book_id, title, amount, trade_time,
                     from_account_id, to_account_id):
        return self.add_bill(book_id, self.BILL_TRANSFER, title, trade_time,
                             from_account_id, amount=amount,
                             to_account_id=to_account_id)

    # ---- 便捷上下文 -------------------------------------------------------
    def get_context(self, group_id=None):
        self.ensure_login()
        gid = group_id or self.get_default_group_id()
        books = self.get_books(gid) or []
        return {
            "group": self.get_group(gid),
            "book": books[0] if books else None,
            "accounts": self.get_accounts(gid),
        }
