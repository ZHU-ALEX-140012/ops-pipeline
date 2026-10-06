"""`tools/set_author_email.py` 的测试。

⚠️ 这是一个**改写 git 历史**的操作，所以测试的重点不是"它能改成功"，
而是**"它改错了会不会把仓库搞坏"**。

两条不变量：
  1. **只换邮箱**：tree / 姓名 / 时间 / 标题 / 正文全部不许变；
  2. **失败不动引用**：任何一条校验过不去，就一个引用都不许改 ——
     宁可什么都不做，也不能留下半新半旧的历史。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import set_author_email as sae  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "author_email"


def force_rmtree(path: Path) -> None:
    """删掉一棵可能含只读文件的目录。

    ⚠️ Windows 上 git 的对象文件是**只读**的，`shutil.rmtree` 会因此抛
    `PermissionError`，于是临时仓库删不掉、下一个测试的 `mkdir` 直接失败
    （实测踩到：`FileExistsError: 当文件已存在时，无法创建该文件`）。
    所以要先去掉只读属性再删。
    """

    def on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except Exception:  # noqa: BLE001 - 清理失败不该让测试报错
            pass

    shutil.rmtree(path, onerror=on_error)


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = dict(os.environ)
    full_env.update(env or {})
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=full_env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


class AuthorEmailTestBase(unittest.TestCase):
    def setUp(self):
        force_rmtree(TMP)
        self.repo = TMP / "repo"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "测试者")
        git(self.repo, "config", "user.email", "private@qq.com")

    def tearDown(self):
        force_rmtree(TMP)

    def commit(self, filename: str, content: str, message: str, *, name: str = "测试者",
               email: str = "private@qq.com", when: str = "2026-01-01T10:00:00+08:00"):
        (self.repo / filename).write_text(content, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message,
            env={"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                 "GIT_AUTHOR_DATE": when,
                 "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
                 "GIT_COMMITTER_DATE": when})

    def three_commits(self):
        self.commit("a.txt", "1\n", "第一个提交", when="2026-01-01T10:00:00+08:00")
        self.commit("b.txt", "2\n", "第二个提交\n\n带一段正文，\n分了行。",
                    when="2026-01-02T11:30:00+08:00")
        self.commit("c.txt", "3\n", "第三个提交\n\n标题下有内容，末尾还有空行。\n\n",
                    when="2026-01-03T09:15:00+08:00")


class TestRewrite(AuthorEmailTestBase):
    NEW = "someone@users.noreply.github.com"

    def test_replaces_every_email(self):
        self.three_commits()
        count, _ = sae.rewrite_author_email(self.repo, self.NEW)
        self.assertEqual(count, 3)
        self.assertEqual(set(git(self.repo, "log", "--format=%ae").split()), {self.NEW})
        self.assertEqual(set(git(self.repo, "log", "--format=%ce").split()), {self.NEW})

    def test_content_is_untouched(self):
        """⚠️ 最关键的一条：**内容一个字都不许变**。"""
        self.three_commits()
        before = git(self.repo, "rev-parse", "HEAD^{tree}").strip()
        sae.rewrite_author_email(self.repo, self.NEW)
        after = git(self.repo, "rev-parse", "HEAD^{tree}").strip()
        self.assertEqual(before, after)

    def test_names_and_dates_survive(self):
        self.three_commits()
        before = git(self.repo, "log", "--format=%an|%aI|%cn|%cI").splitlines()
        sae.rewrite_author_email(self.repo, self.NEW)
        after = git(self.repo, "log", "--format=%an|%aI|%cn|%cI").splitlines()
        self.assertEqual(before, after)
        self.assertEqual({n.strip() for n in git(self.repo, "log", "--format=%an").splitlines()},
                         {"测试者"})

    def test_messages_survive_including_multiline_and_trailing_blanks(self):
        self.three_commits()
        before = [m.rstrip() for m in
                  git(self.repo, "log", "--format=%B%x00").split("\x00") if m.strip()]
        sae.rewrite_author_email(self.repo, self.NEW)
        after = [m.rstrip() for m in
                 git(self.repo, "log", "--format=%B%x00").split("\x00") if m.strip()]
        self.assertEqual(before, after)

    def test_history_shape_survives(self):
        """父提交关系必须原样保留 —— 不能把三条提交压平或错接。"""
        self.three_commits()
        before = git(self.repo, "rev-list", "--parents", "HEAD").split()
        sae.rewrite_author_email(self.repo, self.NEW)
        after = git(self.repo, "rev-list", "--parents", "HEAD").split()
        self.assertEqual(len(before), len(after))
        # 每条提交的父提交个数要一致（3 条线性提交 -> 0,1,1 个父提交）
        self.assertEqual([len(line.split()) - 1 for line in before],
                         [len(line.split()) - 1 for line in after])

    def test_working_tree_stays_clean(self):
        self.three_commits()
        sae.rewrite_author_email(self.repo, self.NEW)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "",
                         "重写历史不该弄脏工作区")

    def test_dry_run_changes_nothing(self):
        self.three_commits()
        head = git(self.repo, "rev-parse", "HEAD").strip()
        count, _ = sae.rewrite_author_email(self.repo, self.NEW, dry_run=True)
        self.assertEqual(count, 3)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), head,
                         "试跑不能动引用")

    def test_already_correct_email_is_idempotent(self):
        self.three_commits()
        sae.rewrite_author_email(self.repo, self.NEW)
        head = git(self.repo, "rev-parse", "HEAD").strip()
        sae.rewrite_author_email(self.repo, self.NEW)
        # 再跑一次会重建提交（因为时间等元数据一致，哈希可能相同也可能不同），
        # 但内容和邮箱必须不变
        self.assertEqual(set(git(self.repo, "log", "--format=%ae").split()), {self.NEW})
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^{tree}").strip(),
                         git(self.repo, "rev-parse", f"{head}^{{tree}}").strip())


class TestFailureIsSafe(AuthorEmailTestBase):
    def test_verification_failure_moves_no_ref(self):
        """⚠️ 校验不过时必须**一个引用都不动**。

        怎么可靠地造出一个"校验会失败"的情形：篡改校验用的取字段函数，
        让它在读**新**提交的作者名时多返回一个字符 —— 模拟"重建过程中出了 bug、
        姓名被改掉了"。这时重写应当拒绝落地。

        （一开始我用"空邮箱"来造失败，结果 git 直接接受了空邮箱，
        校验也通过 —— 那是测试写得不对，不是代码有问题。所以改成
        直接制造一个校验层面的不一致，这才是要守的那条不变量。）
        """
        self.three_commits()
        head = git(self.repo, "rev-parse", "HEAD").strip()
        emails_before = git(self.repo, "log", "--format=%ae").split()
        originals = set(git(self.repo, "rev-list", "--reverse", "main").split())

        real_field = sae._field

        def tampered(repo, rev, fmt):
            value = real_field(repo, rev, fmt)
            if rev not in originals and fmt == "%an":
                return value + "X"
            return value

        with mock.patch.object(sae, "_field", tampered):
            with self.assertRaises(sae.RewriteError) as ctx:
                sae.rewrite_author_email(self.repo, "x@users.noreply.github.com")

        self.assertIn("没有改动任何引用", str(ctx.exception))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), head,
                         "校验失败时不该移动分支")
        self.assertEqual(git(self.repo, "log", "--format=%ae").split(), emails_before,
                         "校验失败时邮箱也不该变")

    def test_verification_failure_leaves_no_temp_file(self):
        """失败也不该留下临时文件（提交信息那个 tmp）。"""
        self.three_commits()
        originals = set(git(self.repo, "rev-list", "--reverse", "main").split())
        real_field = sae._field

        def tampered(repo, rev, fmt):
            value = real_field(repo, rev, fmt)
            return value + "X" if (rev not in originals and fmt == "%s") else value

        with mock.patch.object(sae, "_field", tampered):
            with self.assertRaises(sae.RewriteError):
                sae.rewrite_author_email(self.repo, "x@users.noreply.github.com")
        self.assertFalse((self.repo / ".git" / "_rewrite_msg.tmp").exists())

    def test_error_message_names_the_problem(self):
        self.three_commits()
        originals = set(git(self.repo, "rev-list", "--reverse", "main").split())
        real_field = sae._field

        def tampered(repo, rev, fmt):
            value = real_field(repo, rev, fmt)
            return value + "X" if (rev not in originals and fmt == "%an") else value

        with mock.patch.object(sae, "_field", tampered):
            with self.assertRaises(sae.RewriteError) as ctx:
                sae.rewrite_author_email(self.repo, "x@users.noreply.github.com")
        self.assertIn("作者姓名变了", str(ctx.exception),
                      "报错要说清是哪一条校验没过")


class TestCliSafety(unittest.TestCase):
    def test_module_documents_the_noreply_form(self):
        """帮助文本里要写清用哪种邮箱 —— 用错登录名提交就关联不上主页。"""
        text = (ROOT / "tools" / "set_author_email.py").read_text(encoding="utf-8")
        self.assertIn("users.noreply.github.com", text)
        self.assertIn("登录名必须完全正确", text)

    def test_entry_point_is_console_safe(self):
        """按项目约定，入口必须先调 make_console_safe（中文 Windows 控制台是 GBK）。"""
        text = (ROOT / "tools" / "set_author_email.py").read_text(encoding="utf-8")
        self.assertIn("make_console_safe", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
