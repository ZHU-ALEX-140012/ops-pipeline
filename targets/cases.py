"""接口回归用例（纯数据，不是代码）。

用例写成数据的好处：
* 加一条用例不用碰运行器；
* 失败明细天然是结构化的（用例名 / 期望 / 实际），AI 归因时能直接读到，
  不用去解析文本报告；
* 断言 DSL 在 ops/core/checks.py 里有独立单测。

断言语法见 ops/core/checks.py 的 run_check()。
"""

from __future__ import annotations

from typing import Any

CASES: list[dict[str, Any]] = [
    {
        "name": "health_contract",
        "path": "/api/health",
        "expect_status": 200,
        "checks": [
            ("field_equals", "status", "ok"),
            ("field_type", "version", "str"),
        ],
    },
    {
        "name": "user_detail_contract",
        "path": "/api/users/1",
        "expect_status": 200,
        "checks": [
            ("field_exists", "name"),
            ("field_type", "name", "str"),
            ("field_type", "id", "int"),
            ("field_exists", "email"),
            ("field_type", "age", "int"),
        ],
    },
    {
        "name": "user_not_found_status",
        "path": "/api/users/9999",
        "expect_status": 404,
        "checks": [
            ("field_exists", "error"),
            ("field_type", "id", "int"),
        ],
    },
    {
        "name": "orders_amount_is_number",
        "path": "/api/orders",
        "expect_status": 200,
        "checks": [
            ("field_type", "orders", "list"),
            ("list_len_gt", "orders", 0),
            ("each_field_type", "orders", "amount", "number"),
            ("each_field_type", "orders", "currency", "str"),
        ],
    },
    {
        "name": "product_detail_contract",
        "path": "/api/products/1",
        "expect_status": 200,
        "checks": [
            ("field_type", "id", "int"),
            ("field_type", "title", "str"),
            ("field_type", "price", "number"),
            ("field_exists", "stock"),
        ],
    },
    {
        "name": "product_not_found_status",
        "path": "/api/products/9999",
        "expect_status": 404,
        "checks": [
            ("field_exists", "error"),
        ],
    },
    {
        "name": "echo_roundtrip",
        "method": "POST",
        "path": "/api/echo",
        "json_body": {"msg": "hi", "n": 7},
        "expect_status": 200,
        "checks": [
            ("field_equals", "msg", "hi"),
            ("field_equals", "n", 7),
            ("field_equals", "echoed", True),
        ],
    },
]
