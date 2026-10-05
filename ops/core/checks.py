"""接口契约断言 DSL（纯函数，无 I/O —— 所以好测）。

为什么不用 pytest：目标是**零第三方依赖**，而且这套 DSL 让用例变成纯数据，
AI 归因时能拿到结构化的失败明细（用例名 / 期望 / 实际），比解析文本报告可靠得多。

每个 check 返回 (是否通过, 人类可读的说明)。
"""

from __future__ import annotations

from typing import Any, Sequence

# 类型名 → 判定函数。注意 int 也算 float（JSON 里 19.0 和 19 都该通过"数字"断言），
# 但 bool 必须排除在外 —— 在 Python 里 bool 是 int 的子类，不排除会造成假通过。
TYPE_CHECKS: dict[str, Any] = {
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "str": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _describe(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def get_path(body: Any, path: str) -> tuple[bool, Any]:
    """按 'a.b.0.c' 取嵌套值。返回 (找到?, 值)。"""
    current = body
    for part in str(path).split("."):
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return False, None
            if not -len(current) <= index < len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def run_check(body: Any, check: Sequence[Any]) -> tuple[bool, str]:
    """执行单条断言。check 形如 ("field_type", "data.id", "int")。"""
    if not check:
        return False, "空断言"
    kind = str(check[0])

    if kind == "field_exists":
        path = str(check[1])
        found, _ = get_path(body, path)
        return (True, f"{path} 存在") if found else (False, f"缺少字段 {path}")

    if kind == "field_type":
        path, spec = str(check[1]), str(check[2])
        found, value = get_path(body, path)
        if not found:
            return False, f"{path} 不存在，无法检查类型"
        predicate = TYPE_CHECKS.get(spec)
        if predicate is None:
            return False, f"未知的类型断言 {spec!r}"
        if predicate(value):
            return True, f"{path} 类型为 {spec} ✓"
        return False, f"{path} 期望类型 {spec}，实际 {type(value).__name__}（{_describe(value)}）"

    if kind == "field_equals":
        path, expected = str(check[1]), check[2]
        found, value = get_path(body, path)
        if not found:
            return False, f"{path} 不存在，无法比较"
        if value == expected:
            return True, f"{path} == {_describe(expected)} ✓"
        return False, f"{path} 期望 {_describe(expected)}，实际 {_describe(value)}"

    if kind == "list_len_gt":
        path, minimum = str(check[1]), int(check[2])
        found, value = get_path(body, path)
        if not found:
            return False, f"{path} 不存在"
        if not isinstance(value, list):
            return False, f"{path} 不是数组（{type(value).__name__}）"
        if len(value) > minimum:
            return True, f"{path} 长度 {len(value)} > {minimum} ✓"
        return False, f"{path} 长度 {len(value)}，期望 > {minimum}"

    if kind == "each_field_type":
        path, field, spec = str(check[1]), str(check[2]), str(check[3])
        found, value = get_path(body, path)
        if not found or not isinstance(value, list):
            return False, f"{path} 不存在或不是数组"
        predicate = TYPE_CHECKS.get(spec)
        if predicate is None:
            return False, f"未知的类型断言 {spec!r}"
        bad = [
            (index, item.get(field) if isinstance(item, dict) else None)
            for index, item in enumerate(value)
            if not (isinstance(item, dict) and field in item and predicate(item[field]))
        ]
        if not bad:
            return True, f"{path}[*].{field} 全部为 {spec} ✓"
        index, sample = bad[0]
        return False, (
            f"{path}[{index}].{field} 期望类型 {spec}，实际 {type(sample).__name__}"
            f"（{_describe(sample)}）；共 {len(bad)}/{len(value)} 项不符"
        )

    return False, f"未知断言类型 {kind!r}"


def run_checks(body: Any, checks: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    """执行一组断言，返回逐条结果（含失败原因）。"""
    results: list[dict[str, Any]] = []
    for check in checks or []:
        ok, message = run_check(body, check)
        results.append({"check": list(check), "ok": ok, "message": message})
    return results
