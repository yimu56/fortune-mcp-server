#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多租户凭据库：平台用户 → Fortune 记账账号的映射与加密存储。

存储结构（users.json）：
{
  "version": 1,
  "users": {
    "demo_user": {
      "fortune_username": "demo_user",
      "secret": "<base64: nonce + 密文 + 校验标签>",
      "base_url": null,
      "enabled": true,
      "note": "示例用户 / 研发部",
      "updated": "2026-11-25 21:00:00"
    }
  }
}

加密说明：
  标准库没有 AES，这里用 HMAC-SHA256 派生密钥流 + 加密后再 MAC（encrypt-then-MAC）：
    keystream_i = HMAC(key, "ks" || nonce || counter_i)
    ciphertext  = plaintext XOR keystream
    tag         = HMAC(key, "mac" || nonce || ciphertext)[:16]
  16 字节随机 nonce，逐条独立；密钥取自 FORTUNE_CRED_KEY 或同目录的 *.key 文件（0600）。
  这种构造适合“同机静态保护”的场景；如需更强保证，可改接 KMS / Vault。
"""

import base64
import csv
import hashlib
import hmac
import json
import os
import secrets
import time

DEFAULT_USERS_FILE = "/var/lib/fortune-mcp/users.json"


def _chown_to_dir_owner(path):
    """root 运行 useradmin 时，把新写文件归还给服务用户（数据目录所有者）。

    服务以 fortune 用户运行（0600 文件读不了 root 属主的），CLI 却常用
    sudo 跑，会把 users.json / *.key 写成 root:root，导致服务重启即崩。
    这里在 root 写入后按目录属主纠正，非 root 或目录本就是 root 时不动。
    """
    try:
        if os.geteuid() != 0:
            return
        dpath = os.path.dirname(os.path.abspath(path))
        dir_stat = os.stat(dpath)
        if dir_stat.st_uid == 0:      # 目录属主也是 root，无从推断服务用户
            return
        if os.stat(path).st_uid == 0:
            os.chown(path, dir_stat.st_uid, dir_stat.st_gid)
    except Exception:
        pass


class UserStoreError(Exception):
    """凭据库读写或解密失败。"""


def _b64e(raw):
    return base64.b64encode(raw).decode("ascii")


def _b64d(text):
    return base64.b64decode(text.encode("ascii"))


class Cipher:
    """基于 HMAC-SHA256 的加密器（key 32 字节）。"""

    def __init__(self, key):
        if len(key) < 16:
            raise UserStoreError("加密密钥至少 16 字节")
        self.key = key

    @classmethod
    def load(cls, key_text=None, key_file=None):
        if key_text:
            raw = key_text.strip()
            for decoder in (bytes.fromhex, _b64d):
                try:
                    key = decoder(raw)
                    if len(key) >= 16:
                        return cls(key)
                except Exception:
                    continue
            raise UserStoreError("FORTUNE_CRED_KEY 格式无效（需 hex 或 base64，至少 16 字节）")
        if key_file and os.path.exists(key_file):
            try:
                with open(key_file, "rb") as f:
                    raw = f.read()
            except OSError as exc:
                raise UserStoreError(
                    "密钥文件不可读：%s（%s）。修正：sudo chown fortune:fortune %s "
                    "&& sudo chmod 600 %s" % (key_file, exc, key_file, key_file))
            if raw.startswith(b"hex:"):
                key = bytes.fromhex(raw[4:].decode().strip())
            elif len(raw.strip()) == 64 and all(c in b"0123456789abcdefABCDEF"
                                                for c in raw.strip()):
                key = bytes.fromhex(raw.strip().decode())
            else:
                key = raw.rstrip(b"\r\n")
            if len(key) >= 16:
                return cls(key)
            raise UserStoreError("密钥文件内容无效：%s" % key_file)
        if not key_file:
            raise UserStoreError("未提供加密密钥")
        key = secrets.token_bytes(32)
        key_dir = os.path.dirname(os.path.abspath(key_file))
        try:
            os.makedirs(key_dir, exist_ok=True)
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
        except OSError as exc:
            raise UserStoreError(
                "无法写入密钥文件 %s（%s）。修正：sudo mkdir -p %s "
                "&& sudo chown fortune:fortune %s" % (key_file, exc, key_dir, key_dir))
        _chown_to_dir_owner(key_file)
        return cls(key)

    def _keystream(self, nonce, length):
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hmac.new(self.key, b"ks" + nonce + counter.to_bytes(8, "big"),
                             hashlib.sha256).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encrypt(self, plaintext):
        data = plaintext.encode("utf-8")
        nonce = secrets.token_bytes(16)
        stream = self._keystream(nonce, len(data))
        cipher = bytes(a ^ b for a, b in zip(data, stream))
        tag = hmac.new(self.key, b"mac" + nonce + cipher, hashlib.sha256).digest()[:16]
        return _b64e(nonce + cipher + tag)

    def decrypt(self, token):
        try:
            blob = _b64d(token)
        except Exception:
            raise UserStoreError("密文格式错误")
        if len(blob) < 33:
            raise UserStoreError("密文长度异常")
        nonce, cipher, tag = blob[:16], blob[16:-16], blob[-16:]
        expect = hmac.new(self.key, b"mac" + nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expect):
            raise UserStoreError("密文校验失败（密钥不匹配或数据被篡改）")
        stream = self._keystream(nonce, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")


class UserStore:
    def __init__(self, path=None, key_text=None, key_file=None, base_url=None):
        self.path = os.path.abspath(path or os.environ.get("FORTUNE_USERS_FILE")
                                    or DEFAULT_USERS_FILE)
        self.base_url = base_url or os.environ.get("FORTUNE_BASE_URL") or ""
        self.cipher = Cipher.load(
            key_text=key_text or os.environ.get("FORTUNE_CRED_KEY"),
            key_file=key_file or os.environ.get("FORTUNE_CRED_KEY_FILE")
            or (self.path + ".key"))
        self._data = self._load()

    # ---- 读写 -------------------------------------------------------------
    def _load(self):
        if not os.path.exists(self.path):
            return {"version": 1, "users": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise UserStoreError("读取 %s 失败：%s" % (self.path, exc))
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise UserStoreError("%s 结构不正确" % self.path)
        return data

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass
        _chown_to_dir_owner(self.path)

    # ---- 查询 -------------------------------------------------------------
    def platform_users(self):
        return sorted(self._data["users"].keys())

    def get(self, platform_user):
        return self._data["users"].get(self._normalize(platform_user))

    @staticmethod
    def _normalize(name):
        return (name or "").strip().lower()

    def creds_for(self, platform_user):
        """返回该平台用户的记账凭据；未绑定/已禁用时抛异常。"""
        key = self._normalize(platform_user)
        record = self._data["users"].get(key)
        if record is None:
            raise UserStoreError(
                "当前用户（%s）还没有绑定记账账号。请管理员执行："
                "python3 useradmin.py add --user %s --fortune-user <记账账号> "
                "--password-stdin" % (platform_user, platform_user))
        if not record.get("enabled", True):
            raise UserStoreError("当前用户（%s）的记账账号已被停用，请联系管理员" % platform_user)
        try:
            password = self.cipher.decrypt(record["secret"])
        except UserStoreError as exc:
            raise UserStoreError("账号（%s）凭据解密失败：%s" % (platform_user, exc))
        return {
            "base_url": record.get("base_url") or self.base_url,
            "username": record["fortune_username"],
            "password": password,
            "encrypted_password": "",
            "token": "",
        }

    # ---- 变更 -------------------------------------------------------------
    def upsert(self, platform_user, fortune_username, password, base_url=None,
               note=None, enabled=True):
        key = self._normalize(platform_user)
        if not key:
            raise UserStoreError("平台用户标识不能为空")
        if not fortune_username:
            raise UserStoreError("记账账号不能为空")
        if not password:
            raise UserStoreError("记账密码不能为空")
        self._data["users"][key] = {
            "fortune_username": fortune_username.strip(),
            "secret": self.cipher.encrypt(password),
            "base_url": (base_url or None),
            "enabled": bool(enabled),
            "note": note or "",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save()
        return key

    def set_enabled(self, platform_user, enabled):
        key = self._normalize(platform_user)
        if key not in self._data["users"]:
            raise UserStoreError("未找到用户：%s" % platform_user)
        self._data["users"][key]["enabled"] = bool(enabled)
        self._data["users"][key]["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()

    def remove(self, platform_user):
        key = self._normalize(platform_user)
        if key not in self._data["users"]:
            raise UserStoreError("未找到用户：%s" % platform_user)
        self._data["users"].pop(key)
        self._save()

    def import_csv(self, csv_path):
        """批量导入，列名：platform_user,fortune_username,password,note[base_url]。"""
        added, updated, failed = 0, 0, []
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            required = {"platform_user", "fortune_username", "password"}
            if not required.issubset({(c or "").strip() for c in reader.fieldnames or []}):
                raise UserStoreError("CSV 表头需包含：platform_user,fortune_username,password,note")
            for row in reader:
                row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
                if not row.get("platform_user"):
                    continue
                existed = self._normalize(row["platform_user"]) in self._data["users"]
                try:
                    self.upsert(row["platform_user"], row.get("fortune_username"),
                                row.get("password"), base_url=row.get("base_url") or None,
                                note=row.get("note") or "")
                    if existed:
                        updated += 1
                    else:
                        added += 1
                except Exception as exc:
                    failed.append("%s: %s" % (row.get("platform_user"), exc))
        return added, updated, failed

    @staticmethod
    def mask(fortune_username):
        name = fortune_username or ""
        if len(name) <= 2:
            return name[:1] + "*"
        return name[0] + "*" * (len(name) - 2) + name[-1]
