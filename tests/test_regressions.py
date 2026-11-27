#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试（纯标准库 unittest，不需要下游记账系统）。

运行：
    python3 -m unittest discover -s tests -v

每个用例对应一条已修复的缺陷，修复前运行会失败。
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 测试过程不落盘 token 缓存
os.environ.setdefault("FORTUNE_TOKEN_CACHE", "0")

import fortune_tools  # noqa: E402
import server  # noqa: E402
import user_store  # noqa: E402
from fortune_api import FortuneError  # noqa: E402


class FakeClient:
    """只提供名称解析用到的查询方法，不发起任何网络请求。"""

    def __init__(self, accounts=(), categories=()):
        self._accounts = list(accounts)
        self._categories = list(categories)

    def get_accounts(self, group_id):
        return self._accounts

    def get_categories(self, book_id, bill_type=1):
        return self._categories


ACCOUNTS = ({"accountId": 1, "accountName": "现金"},
            {"accountId": 2, "accountName": "备用现金"},
            {"accountId": 3, "accountName": "工资卡一"},
            {"accountId": 4, "accountName": "工资卡二"})
# 「早餐」与子分类同名（完全匹配优先）；「交通出行」「交通补贴」共前缀（多命中）
CATEGORIES = ({"categoryId": 10, "categoryName": "餐饮",
               "children": [{"categoryId": 11, "categoryName": "早餐"}]},
              {"categoryId": 20, "categoryName": "早餐补贴"},
              {"categoryId": 30, "categoryName": "交通出行"},
              {"categoryId": 31, "categoryName": "交通补贴"})


class UserStoreReloadTest(unittest.TestCase):
    """凭据库被其他进程改写后，运行中的实例无需重启即可看到。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "users.json")
        self.running = user_store.UserStore(path=self.path)   # 模拟常驻服务
        self.running.upsert("alice", "u_alice", "pw_alice")
        self.admin = user_store.UserStore(path=self.path)     # 模拟 useradmin.py

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_user_visible_without_restart(self):
        self.admin.upsert("bob", "u_bob", "pw_bob")
        self.assertEqual(self.running.creds_for("bob")["username"], "u_bob")

    def test_disabled_user_rejected_without_restart(self):
        self.admin.set_enabled("alice", False)
        with self.assertRaisesRegex(user_store.UserStoreError, "已被停用"):
            self.running.creds_for("alice")

    def test_removed_user_rejected_without_restart(self):
        self.admin.remove("alice")
        with self.assertRaises(user_store.UserStoreError):
            self.running.creds_for("alice")

    def test_corrupted_file_keeps_previous_data(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.running.platform_users(), ["alice"])

    def test_unchanged_file_not_reloaded(self):
        before = self.running._data
        self.running.platform_users()
        self.assertIs(self.running._data, before)


class AccountNameMatchTest(unittest.TestCase):

    def setUp(self):
        self.client = FakeClient(accounts=ACCOUNTS)

    def test_multiple_matches_rejected(self):
        with self.assertRaisesRegex(FortuneError, "匹配到多个账户.*工资卡一\\(3\\).*工资卡二\\(4\\)"):
            fortune_tools._resolve_account(self.client, 1, "工资卡")

    def test_exact_name_wins(self):
        aid, _ = fortune_tools._resolve_account(self.client, 1, "现金")
        self.assertEqual(aid, 1)

    def test_exact_name_beats_longer_names(self):
        # 名称同时是其他账户名的子串时，完全同名的一项优先，不算多命中
        self.assertEqual(fortune_tools._resolve_account(self.client, 1, "现金")[0], 1)

    def test_unique_partial_match_resolved(self):
        aid, _ = fortune_tools._resolve_account(self.client, 1, "备用")
        self.assertEqual(aid, 2)

    def test_unknown_name_rejected(self):
        with self.assertRaisesRegex(FortuneError, "未找到账户"):
            fortune_tools._resolve_account(self.client, 1, "公积金")

    def test_foreign_id_rejected(self):
        with self.assertRaisesRegex(FortuneError, "不属于当前用户"):
            fortune_tools._resolve_account(self.client, 1, 999)


class CategoryNameMatchTest(unittest.TestCase):

    def setUp(self):
        self.client = FakeClient(categories=CATEGORIES)

    def test_multiple_matches_rejected(self):
        with self.assertRaisesRegex(FortuneError, "匹配到多个分类.*交通出行\\(30\\).*交通补贴\\(31\\)"):
            fortune_tools._resolve_category(self.client, 1, 1, "交通")

    def test_exact_name_wins(self):
        self.assertEqual(
            fortune_tools._resolve_category(self.client, 1, 1, "早餐"), 11)

    def test_unique_partial_match_resolved(self):
        self.assertEqual(
            fortune_tools._resolve_category(self.client, 1, 1, "餐饮"), 10)
        self.assertEqual(
            fortune_tools._resolve_category(self.client, 1, 1, "餐补"), 20)


class AmountValidationTest(unittest.TestCase):

    def test_non_finite_rejected(self):
        for bad in ("nan", "NaN", "inf", "-inf", float("nan"), float("inf")):
            with self.subTest(value=bad), self.assertRaises(FortuneError):
                fortune_tools._positive_amount(bad)

    def test_non_positive_rejected(self):
        for bad in (0, -1, "0.00", "", None):
            with self.subTest(value=bad), self.assertRaises(FortuneError):
                fortune_tools._positive_amount(bad)

    def test_valid_amount_rounded(self):
        self.assertEqual(fortune_tools._positive_amount("12.5"), 12.5)
        self.assertEqual(fortune_tools._positive_amount(3), 3.0)


class HealthSummaryTest(unittest.TestCase):
    """免鉴权的 /healthz 不能带出记账登录名。"""

    def setUp(self):
        self.cfg = server.Config()
        self.cfg.username = "login_name_for_test"

    def test_default_summary_hides_login_name(self):
        summary = json.dumps(self.cfg.summary(), ensure_ascii=False)
        self.assertNotIn("login_name_for_test", summary)
        self.assertEqual(self.cfg.summary()["username"], "(已配置)")

    def test_unconfigured_account_reported(self):
        self.cfg.username = ""
        self.assertEqual(self.cfg.summary()["username"], "(未配置)")

    def test_self_check_can_reveal_login_name(self):
        summary = self.cfg.summary(reveal_user=True)
        self.assertEqual(summary["username"], "login_name_for_test")


if __name__ == "__main__":
    unittest.main()
