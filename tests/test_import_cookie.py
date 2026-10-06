"""从 cURL 导入 Cookie 的测试。

⚠️ 这组测试是**真实踩坑换来的**：我一开始让用户用控制台
`copy(document.cookie)` 取 Cookie，结果拿不到京东的登录凭证 ——
`pt_key` / `pt_pin` 是 **HttpOnly**，浏览器故意不给网页脚本看。

能拿到 HttpOnly Cookie 的只有"浏览器自己导出请求"这条路
（复制为 cURL / 网络面板的请求标头）。但那种文本很长，
人手工从里面挑 Cookie 极易挑错 —— 这个工具就是替人做这一步。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import import_cookie  # noqa: E402
from ops.core import config as cfgmod  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "import_cookie"

GOOD_COOKIE = ("__jdv=76161171|baidu|-|utm_source=x; pt_key=AAJfakeKEYvalue; "
               "pt_pin=jd_fakeuser; wlfstk_smdl=abcdef")

CURL_BASH = (
    "curl 'https://item.m.jd.com/product/100254961843.html' \\\n"
    "  -H 'accept: text/html' \\\n"
    f"  -H 'cookie: {GOOD_COOKIE}' \\\n"
    "  -H 'user-agent: Mozilla/5.0' \\\n"
    "  --compressed"
)
CURL_BASH_DOUBLE = (
    "curl \"https://item.m.jd.com/product/1.html\" "
    "-H \"accept: text/html\" "
    f"-H \"cookie: {GOOD_COOKIE}\""
)
CURL_WITH_B = f"curl 'https://item.m.jd.com/product/1.html' -b '{GOOD_COOKIE}'"
RAW_HEADERS = (
    "GET /product/1.html HTTP/1.1\n"
    "Host: item.m.jd.com\n"
    f"cookie: {GOOD_COOKIE}\n"
    "accept: text/html\n"
)


class TestExtractCookie(unittest.TestCase):
    def test_extracts_from_chrome_bash_curl(self):
        self.assertEqual(import_cookie.extract_cookie(CURL_BASH), GOOD_COOKIE)

    def test_extracts_from_double_quoted_headers(self):
        self.assertEqual(import_cookie.extract_cookie(CURL_BASH_DOUBLE), GOOD_COOKIE)

    def test_extracts_from_curl_b_flag(self):
        self.assertEqual(import_cookie.extract_cookie(CURL_WITH_B), GOOD_COOKIE)

    def test_extracts_from_raw_request_headers(self):
        self.assertEqual(import_cookie.extract_cookie(RAW_HEADERS), GOOD_COOKIE)

    def test_accepts_a_bare_cookie_string(self):
        """用户可能直接把 Cookie 值粘进来了 —— 也该收下。"""
        self.assertEqual(import_cookie.extract_cookie(GOOD_COOKIE), GOOD_COOKIE)

    def test_returns_none_when_there_is_no_cookie(self):
        self.assertIsNone(import_cookie.extract_cookie(
            "curl 'https://x/' -H 'accept: text/html' --compressed"))
        self.assertIsNone(import_cookie.extract_cookie(""))
        self.assertIsNone(import_cookie.extract_cookie("随便一段话，没有等号"))

    def test_rejects_text_that_only_looks_like_a_pair(self):
        """`name=value` 太宽松了，不能把一整段 HTML 当成 Cookie。"""
        self.assertIsNone(import_cookie.extract_cookie(
            "curl 'https://x/' -H 'accept: text/html;q=0.9'"))
        self.assertIsNone(import_cookie.extract_cookie('<a href="x?a=1">y</a>'))

    def test_does_not_confuse_other_headers_with_cookie(self):
        text = ("curl 'https://x/' -H 'accept: text/html' "
                f"-H 'cookie: {GOOD_COOKIE}' -H 'referer: https://item.jd.com/'")
        self.assertEqual(import_cookie.extract_cookie(text), GOOD_COOKIE)

    def test_summary_lists_names_without_values(self):
        """摘要**只列名字、不含值** —— 这样用户截图求助也不会泄露凭据。"""
        text = import_cookie.summarize(GOOD_COOKIE)
        self.assertIn("pt_key", text)
        self.assertIn("pt_pin", text)
        self.assertNotIn("AAJfakeKEYvalue", text)
        self.assertNotIn("jd_fakeuser", text)


class TestDetectsOverwrittenClipboard(unittest.TestCase):
    """⚠️ **这条测试是实测换来的。**

    用户先在浏览器里复制好 cURL，然后**又复制了一遍要运行的那条命令**，
    剪贴板被覆盖。工具只报"没找到 Cookie"，用户完全看不出问题在哪 ——
    实际那次剪贴板里正好是 `python tools\\import_cookie.py`（30 个字符）。
    所以这里要单独认出"剪贴板里是命令"，并直接点明原因与正确顺序。
    """

    def test_recognizes_the_exact_command_that_fooled_us(self):
        self.assertTrue(import_cookie.looks_like_a_shell_command(
            r"python tools\import_cookie.py"))

    def test_recognizes_other_commands(self):
        for text in ("python -m ops --task price_watch --once",
                     "powershell -Command Get-Clipboard",
                     r"scripts\set-secret.cmd -Name OPS_X",
                     "curl 'https://x/'"):
            with self.subTest(text=text):
                self.assertTrue(import_cookie.looks_like_a_shell_command(text))

    def test_does_not_flag_a_real_curl_export(self):
        self.assertFalse(import_cookie.looks_like_a_shell_command(CURL_BASH))

    def test_does_not_flag_a_bare_cookie_string(self):
        self.assertFalse(import_cookie.looks_like_a_shell_command(GOOD_COOKIE))

    def test_does_not_flag_empty(self):
        self.assertFalse(import_cookie.looks_like_a_shell_command(""))


class TestWriteSecret(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "config").mkdir(parents=True, exist_ok=True)
        (TMP / "config" / "config.toml").write_text(
            '[project]\nroot = ""\n', encoding="utf-8")
        self.cfg = cfgmod.load_config(str(TMP / "config" / "config.toml"))

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_writes_and_reads_back(self):
        path = cfgmod.write_secret(self.cfg, "OPS_X", "abc=1")
        self.assertTrue(Path(path).is_file())
        again = cfgmod.load_config(str(TMP / "config" / "config.toml"))
        self.assertEqual(again["_secrets"]["OPS_X"], "abc=1")

    def test_replaces_same_key_instead_of_duplicating(self):
        """同名键必须**替换**：留下两条的话，读到哪条就全看运气了。"""
        cfgmod.write_secret(self.cfg, "OPS_X", "old")
        cfgmod.write_secret(self.cfg, "OPS_X", "new")
        text = (TMP / "config" / "secrets.local.toml").read_text(encoding="utf-8")
        self.assertEqual(text.count("OPS_X"), 1)
        self.assertIn('"new"', text)

    def test_keeps_other_keys(self):
        cfgmod.write_secret(self.cfg, "A", "1")
        cfgmod.write_secret(self.cfg, "B", "2")
        cfgmod.write_secret(self.cfg, "A", "3")
        again = cfgmod.load_config(str(TMP / "config" / "config.toml"))
        self.assertEqual(again["_secrets"]["A"], "3")
        self.assertEqual(again["_secrets"]["B"], "2")

    def test_round_trips_a_realistic_cookie(self):
        cfgmod.write_secret(self.cfg, "OPS_JD_COOKIE", GOOD_COOKIE)
        again = cfgmod.load_config(str(TMP / "config" / "config.toml"))
        self.assertEqual(again["_secrets"]["OPS_JD_COOKIE"], GOOD_COOKIE)

    def test_no_bom_so_powershell_reads_it_the_same_way(self):
        """set-secret.ps1 读的是 UTF-8 无 BOM 的文本；带 BOM 会让第一个键名读错。"""
        path = cfgmod.write_secret(self.cfg, "OPS_X", "1")
        raw = Path(path).read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_matches_the_format_set_secret_ps1_writes(self):
        cfgmod.write_secret(self.cfg, "OPS_X", "1")
        text = (TMP / "config" / "secrets.local.toml").read_text(encoding="utf-8")
        self.assertIn('OPS_X = "1"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
