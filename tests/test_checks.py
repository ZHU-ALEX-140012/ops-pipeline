"""断言 DSL 测试。

这个 DSL 是回归流水线的判据，它自己有 bug 的话，整套回归就是假绿。
所以这里对每种断言都覆盖"该通过"和"该失败"两侧。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import checks  # noqa: E402


class TestGetPath(unittest.TestCase):
    def test_nested(self):
        found, value = checks.get_path({"a": {"b": [10, 20]}}, "a.b.1")
        self.assertTrue(found)
        self.assertEqual(value, 20)

    def test_missing_key(self):
        found, _ = checks.get_path({"a": {}}, "a.b")
        self.assertFalse(found)

    def test_index_out_of_range(self):
        found, _ = checks.get_path({"a": [1]}, "a.5")
        self.assertFalse(found)

    def test_scalar_traversal(self):
        found, _ = checks.get_path({"a": 1}, "a.b")
        self.assertFalse(found)


class TestTypeChecks(unittest.TestCase):
    def test_field_type_ok(self):
        ok, msg = checks.run_check({"id": 7}, ("field_type", "id", "int"))
        self.assertTrue(ok, msg)

    def test_bool_is_not_int(self):
        """Python 里 bool 是 int 的子类，不排除会造成假通过。"""
        ok, msg = checks.run_check({"flag": True}, ("field_type", "flag", "int"))
        self.assertFalse(ok, msg)
        self.assertIn("bool", msg)

    def test_int_passes_number_assertion(self):
        """JSON 里 19.0 和 19 都该通过"数字"断言。"""
        ok, _ = checks.run_check({"amount": 19}, ("field_type", "amount", "number"))
        self.assertTrue(ok)

    def test_string_fails_number_assertion(self):
        """这正是注入回归 #2：金额被序列化成字符串。"""
        ok, msg = checks.run_check({"amount": "19.90"}, ("field_type", "amount", "number"))
        self.assertFalse(ok)
        self.assertIn("str", msg)

    def test_unknown_type_spec(self):
        ok, msg = checks.run_check({"a": 1}, ("field_type", "a", "integer"))
        self.assertFalse(ok)
        self.assertIn("未知", msg)


class TestOtherChecks(unittest.TestCase):
    def test_field_exists(self):
        self.assertTrue(checks.run_check({"name": "x"}, ("field_exists", "name"))[0])
        self.assertFalse(checks.run_check({"full_name": "x"}, ("field_exists", "name"))[0])

    def test_field_equals(self):
        self.assertTrue(checks.run_check({"status": "ok"}, ("field_equals", "status", "ok"))[0])
        ok, msg = checks.run_check({"status": "down"}, ("field_equals", "status", "ok"))
        self.assertFalse(ok)
        self.assertIn("down", msg)

    def test_list_len_gt(self):
        self.assertTrue(checks.run_check({"o": [1, 2]}, ("list_len_gt", "o", 1))[0])
        self.assertFalse(checks.run_check({"o": [1]}, ("list_len_gt", "o", 1))[0])

    def test_each_field_type_all_good(self):
        body = {"orders": [{"amount": 1.5}, {"amount": 2}]}
        self.assertTrue(checks.run_check(body, ("each_field_type", "orders", "amount", "number"))[0])

    def test_each_field_type_reports_first_offender(self):
        body = {"orders": [{"amount": 1.5}, {"amount": "2.00"}, {"amount": "3.00"}]}
        ok, msg = checks.run_check(body, ("each_field_type", "orders", "amount", "number"))
        self.assertFalse(ok)
        self.assertIn("orders[1].amount", msg)
        self.assertIn("2/3", msg)

    def test_each_field_type_missing_field(self):
        ok, msg = checks.run_check({"orders": [{"id": 1}]}, ("each_field_type", "orders", "amount", "number"))
        self.assertFalse(ok)
        self.assertIn("orders[0].amount", msg)


class TestRunChecks(unittest.TestCase):
    def test_collects_all_results(self):
        results = checks.run_checks(
            {"id": 1, "name": "alice"},
            [("field_type", "id", "int"), ("field_type", "name", "str"), ("field_exists", "email")],
        )
        self.assertEqual(len(results), 3)
        self.assertEqual([r["ok"] for r in results], [True, True, False])

    def test_empty_checks(self):
        self.assertEqual(checks.run_checks({"a": 1}, []), [])


class TestRealCasesAgainstRealPayloads(unittest.TestCase):
    """用真实靶子的响应体验证用例本身是对的 —— 防止"用例写错导致假绿/假红"。"""

    def test_v1_payloads_pass(self):
        from targets import cases as cases_module

        # v1 的真实响应形状
        payloads = {
            "/api/health": {"status": "ok", "version": "v1"},
            "/api/users/1": {"id": 1, "name": "alice", "email": "a@e.com", "age": 30},
            "/api/users/9999": {"error": "user not found", "id": 9999},
            "/api/orders": {"orders": [{"id": 1001, "amount": 19.9, "currency": "CNY"}]},
            "/api/products/1": {"id": 1, "title": "widget", "price": 9.9, "stock": 12},
            "/api/products/9999": {"error": "product not found", "id": 9999},
            "/api/echo": {"msg": "hi", "n": 7, "echoed": True},
        }
        for case in cases_module.CASES:
            body = payloads[case["path"]]
            failures = [c for c in checks.run_checks(body, case.get("checks")) if not c["ok"]]
            self.assertEqual(failures, [], f"v1 下用例 {case['name']} 不该失败：{failures}")

    def test_v2_injected_regressions_are_caught(self):
        """三类注入回归必须被对应用例抓到，且不影响其他用例。"""
        from targets import cases as cases_module

        v2_payloads = {
            "/api/health": {"status": "ok", "version": "v2"},
            "/api/users/1": {"id": 1, "full_name": "alice", "email": "a@e.com", "age": 30},
            "/api/users/9999": {"error": "user not found", "id": 9999},
            "/api/orders": {"orders": [{"id": 1001, "amount": "19.90", "currency": "CNY"}]},
            "/api/products/1": {"id": 1, "title": "widget", "price": 9.9, "stock": 12},
            # 回归 3 是状态码问题，payload 本身正常，由 expect_status 抓
            "/api/products/9999": {"product": None},
            "/api/echo": {"msg": "hi", "n": 7, "echoed": True},
        }
        expected_failures = {
            "user_detail_contract",
            "orders_amount_is_number",
            "product_not_found_status",
        }
        actual_failures = set()
        for case in cases_module.CASES:
            body = v2_payloads[case["path"]]
            # 回归 3 的 v2 返回 200 而不是 404，这里按运行器的规则一并判断
            status_mismatch = case["path"] == "/api/products/9999"
            failures = [c for c in checks.run_checks(body, case.get("checks")) if not c["ok"]]
            if failures or status_mismatch:
                actual_failures.add(case["name"])
            self.assertEqual(
                bool(failures), case["name"] in expected_failures,
                f"用例 {case['name']} 的失败与否不符合预期",
            )
        self.assertEqual(actual_failures, expected_failures)


if __name__ == "__main__":
    unittest.main(verbosity=2)
