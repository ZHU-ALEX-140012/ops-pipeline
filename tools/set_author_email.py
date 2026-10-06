"""重写仓库里所有提交的作者/提交者**邮箱**，保留姓名、时间与全部内容。

    python tools/set_author_email.py --email ZHU-ALEX-140012@users.noreply.github.com
    python tools/set_author_email.py --email me@example.com --dry-run

## 为什么需要这个

提交里的作者邮箱会**跟着仓库公开**。用私人邮箱（Gmail / QQ 之类）推公开仓库，
等于把邮箱挂在网上招揽垃圾邮件。GitHub 提供了不公开的替代地址：

    <用户名>@users.noreply.github.com

真名留着（简历需要），邮箱换成它。**登录名必须完全正确** ——
用错了提交就关联不到你的 GitHub 主页，简历上那个贡献记录也就没了。

## 为什么不用 `git filter-branch`

`filter-branch` 的 `--env-filter` 要调 `sh`。在受限环境里 `sh.exe` 可能起不来
（实测：`couldn't create signal pipe`）。这里改用 `git commit-tree` 逐提交重建，
只用 git 本身，不经过 shell。

## 安全性

每个提交都用**原来的 tree、原来的提交信息、原来的父提交（映射后）、
原来的作者/提交者姓名与时间**重建，只换邮箱。

重建完全部**逐条校验**（tree / 姓名 / 时间 / 标题 / 正文都必须不变），
任何一条不通过就报错退出、**不动任何引用** —— 宁可什么都不做，也不能留下
一个半新半旧的历史。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class RewriteError(RuntimeError):
    """重写失败。失败时保证没有改动任何引用。"""


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)
    if proc.returncode != 0:
        raise RewriteError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


def _field(repo: Path, rev: str, fmt: str) -> str:
    return _git(repo, "log", "-1", f"--format={fmt}", rev).rstrip("\n")


def _normalize(message: str) -> str:
    """比较提交信息前先规范尾部空白。

    `commit-tree -F` 会去掉消息尾部的空行，而 `%B` 输出时补一个换行，
    于是两边永远差一个 `\\n`。那是无意义的差异；
    除尾部空白之外的任何差别都必须报出来。
    """
    return message.rstrip()


def rewrite_author_email(repo: Path, email: str, *, branch: str = "main",
                         dry_run: bool = False) -> tuple[int, list[str]]:
    """把 `branch` 上所有提交的作者与提交者邮箱换成 `email`。

    返回 `(重写的提交数, 新提交号列表)`。
    """
    revs = _git(repo, "rev-list", "--reverse", branch).split()
    if not revs:
        return 0, []

    mapping: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    msg_file = repo / ".git" / "_rewrite_msg.tmp"

    for rev in revs:
        tree = _git(repo, "rev-parse", f"{rev}^{{tree}}").strip()
        parents = _git(repo, "rev-list", "--parents", "-n", "1", rev).split()[1:]

        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": _field(repo, rev, "%an"),
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": _field(repo, rev, "%aI"),
            "GIT_COMMITTER_NAME": _field(repo, rev, "%cn"),
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": _field(repo, rev, "%cI"),
        })

        msg_file.write_text(_git(repo, "log", "-1", "--format=%B", rev), encoding="utf-8")

        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", mapping[parent]]
        args += ["-F", str(msg_file)]
        new_rev = _git(repo, *args, env=env).strip()

        mapping[rev] = new_rev
        pairs.append((rev, new_rev))

    msg_file.unlink(missing_ok=True)

    # ---------------------------------------------------------------- 校验
    problems: list[str] = []
    for old, new in pairs:
        checks = [
            (f"{old[:8]} 内容（tree）变了",
             _git(repo, "rev-parse", f"{old}^{{tree}}") != _git(repo, "rev-parse", f"{new}^{{tree}}")),
            (f"{old[:8]} 作者姓名变了", _field(repo, old, "%an") != _field(repo, new, "%an")),
            (f"{old[:8]} 作者时间变了", _field(repo, old, "%aI") != _field(repo, new, "%aI")),
            (f"{old[:8]} 提交者姓名变了", _field(repo, old, "%cn") != _field(repo, new, "%cn")),
            (f"{old[:8]} 提交者时间变了", _field(repo, old, "%cI") != _field(repo, new, "%cI")),
            (f"{old[:8]} 标题变了", _field(repo, old, "%s") != _field(repo, new, "%s")),
            (f"{old[:8]} 正文变了",
             _normalize(_git(repo, "log", "-1", "--format=%B", old))
             != _normalize(_git(repo, "log", "-1", "--format=%B", new))),
            (f"{old[:8]} 新邮箱没生效",
             _field(repo, new, "%ae") != email or _field(repo, new, "%ce") != email),
        ]
        problems += [label for label, bad in checks if bad]

    if problems:
        raise RewriteError(
            "校验不通过，**没有改动任何引用**：\n  " + "\n  ".join(problems[:20])
        )

    if dry_run:
        return len(pairs), [new for _, new in pairs]

    old_head = _git(repo, "rev-parse", branch).strip()
    _git(repo, "update-ref", f"refs/heads/{branch}", mapping[old_head], old_head)
    return len(pairs), [new for _, new in pairs]


def main(argv: list[str] | None = None) -> int:
    from ops.core import make_console_safe

    make_console_safe()

    parser = argparse.ArgumentParser(description="重写提交的作者邮箱（只换邮箱，不动内容）")
    parser.add_argument("--email", required=True,
                        help="新邮箱，公开仓库请用 <用户名>@users.noreply.github.com")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验不落引用（安全试跑）")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    before = _git(repo, "log", "--format=%ae", args.branch).split()
    print(f"仓库：{repo}")
    print(f"分支：{args.branch}，提交 {len(before)} 个")
    print(f"现有邮箱：{sorted(set(before))}")
    print(f"目标邮箱：{args.email}\n")

    try:
        count, _ = rewrite_author_email(repo, args.email, branch=args.branch,
                                        dry_run=args.dry_run)
    except RewriteError as exc:
        print(f"❌ {exc}")
        return 1

    if args.dry_run:
        print(f"✓ 试跑通过（{count} 个提交），**没有改动任何引用**")
        return 0

    after = set(_git(repo, "log", "--format=%ae", args.branch).split())
    print(f"✓ 已重写 {count} 个提交，现在的邮箱：{sorted(after)}")
    print("\n⚠️ 旧提交仍留在对象库里，建议随后清理：")
    print("   git reflog expire --expire=now --all && git gc --prune=now")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
