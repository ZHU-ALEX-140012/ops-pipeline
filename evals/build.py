"""把 `fixtures.py` 里的场景固化成冻结的黄金集 JSON。

用法：

    python -m evals.build            # 重新生成全部夹具
    python -m evals.build --check    # 只校验，不写盘（CI 用）

**为什么要有这一步**：评测需要一个固定靶子。夹具一旦固化进仓库，
生产代码怎么改都不会让"题目"偷偷变简单 —— 分数变化才只反映模型/提示词的变化。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python evals/build.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import fixtures  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
INDEX_FILE = GOLDEN_DIR / "_index.json"


def render(name: str, doc: dict, expect: dict, note: str) -> str:
    return json.dumps(
        {"name": name, "task": doc["task"], "note": note, "expect": expect, "run_doc": doc},
        ensure_ascii=False, indent=2,
    ) + "\n"


def build(write: bool = True) -> list[str]:
    specs = fixtures.scenario_index()
    problems: list[str] = []
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    names: list[str] = []

    for name, doc, expect in fixtures.build_all():
        note = specs[name]["note"]
        text = render(name, doc, expect, note)
        names.append(name)
        path = GOLDEN_DIR / f"{name}.json"
        if write:
            path.write_text(text, encoding="utf-8")
        elif not path.is_file():
            problems.append(f"缺少夹具文件 {path.name}")
        else:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != json.loads(text):
                problems.append(
                    f"{path.name} 与 SCENARIOS 不一致 —— 改了题目就要重新生成 "
                    f"(`python -m evals.build`)"
                )

    if write:
        INDEX_FILE.write_text(
            json.dumps({"count": len(names), "scenarios": names}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/校验黄金集夹具")
    parser.add_argument("--check", action="store_true", help="只校验，不写盘")
    args = parser.parse_args()

    problems = build(write=not args.check)
    total = len(fixtures.SCENARIOS)
    if problems:
        print(f"[FAIL] 夹具校验未通过（{len(problems)} 个问题）")
        for item in problems:
            print(f"  - {item}")
        return 1
    action = "校验" if args.check else "生成"
    print(f"[OK] {action}完成：{total} 个场景 -> {GOLDEN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
