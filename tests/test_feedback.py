"""人工反馈的测试。

这个模块的价值全在"**别把判错当成判对**"：
* 关键词匹配必须先判负向 —— "不对" 里含 "对"；
* 回复正文里通常带着整段引用原文，不能把引文当反馈内容；
* 主题里的标记是唯一认领依据，认不出来就**静默跳过**而不是猜。

把误报记成"判对了"，比没有反馈更糟 —— 它会让准确率虚高，掩盖真问题。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import feedback as fb  # noqa: E402
from ops.core import store  # noqa: E402

TMP = ROOT / "data" / "_test_tmp" / "feedback"


class TestTokens(unittest.TestCase):
    def test_token_is_last_segment(self):
        self.assertEqual(fb.token_for("20261005T184422Z-api_watch-d6aae4"), "d6aae4")

    def test_mark_subject_adds_token(self):
        marked = fb.mark_subject("[需要注意] 出问题了", "20261005T184422Z-api_watch-d6aae4")
        self.assertIn("#d6aae4", marked)

    def test_mark_subject_is_idempotent(self):
        """回复的主题已经带标记时不能再套一层 —— 否则标记会越滚越长。"""
        once = fb.mark_subject("标题", "run-abc123")
        self.assertEqual(fb.mark_subject(once, "run-abc123"), once)


class TestClassify(unittest.TestCase):
    def test_down_checked_before_up(self):
        """核心用例：'不对' 含 '对'，先判正向就会把误报记成判对。"""
        self.assertEqual(fb.classify("不对"), "down")
        self.assertEqual(fb.classify("判得不对"), "down")
        self.assertEqual(fb.classify("误报"), "down")
        self.assertEqual(fb.classify("👎"), "down")

    def test_up(self):
        self.assertEqual(fb.classify("对"), "up")
        self.assertEqual(fb.classify("正确"), "up")
        self.assertEqual(fb.classify("👍"), "up")
        self.assertEqual(fb.classify("ok"), "up")

    def test_unrelated_returns_none(self):
        """认不出来就不猜 —— 猜错比不认更糟。"""
        self.assertIsNone(fb.classify("收到"))
        self.assertIsNone(fb.classify(""))
        self.assertIsNone(fb.classify("这是一封自动回复"))


class TestFirstMeaningfulLine(unittest.TestCase):
    def test_skips_leading_blank_lines(self):
        self.assertEqual(fb.first_meaningful_line("\n\n  对  \n\n> 引用"), "对")

    def test_skips_quoted_lines(self):
        body = "> 这是原邮件的内容\n> 第二行\n对"
        self.assertEqual(fb.first_meaningful_line(body), "对")

    def test_stops_at_quote_header(self):
        """中文客户端的引用分隔行之后全是原文，不能再往下读。"""
        body = "错\n\n在 2026年10月5日 写道：\n原邮件里说 alert 一切正常"
        self.assertEqual(fb.first_meaningful_line(body), "错")

    def test_real_world_reply_shape(self):
        """真实回复长这样：第一行是人工写的，下面是整段引用。"""
        body = (
            "不对，这个接口本来就下线了\n"
            "\n"
            "在 2026-10-05 18:44, ops-pipeline 写道：\n"
            "> 🟠 需要注意　worldtime 接口连续失败\n"
            "> 目标 2/3 正常\n"
        )
        self.assertEqual(fb.classify(fb.first_meaningful_line(body)), "down")

    def test_strips_bullet_prefix(self):
        self.assertEqual(fb.first_meaningful_line("- 对"), "对")


class TestParseReply(unittest.TestCase):
    def test_parses_token_and_vote(self):
        reply = fb.parse_reply("Re: [需要注意] 出问题了 [#d6aae4]", "对")
        self.assertIsNotNone(reply)
        self.assertEqual(reply.token, "d6aae4")
        self.assertEqual(reply.vote, "up")

    def test_no_token_is_none(self):
        self.assertIsNone(fb.parse_reply("Re: 随便什么标题", "对"))

    def test_token_but_no_vote_is_none(self):
        self.assertIsNone(fb.parse_reply("Re: x [#d6aae4]", "收到，谢谢"))

    def test_quoted_original_does_not_make_it_a_vote(self):
        """只带引用、没写正文的回复不该被当成反馈。

        原邮件里有 `plain_summary` 之类英文词，任意一条都可能撞上关键词。
        """
        body = (
            "在 2026-10-05 写道：\n"
            "> 🟠 需要注意　worldtime 连续失败\n"
            "> 建议：先不用管\n"
        )
        self.assertIsNone(fb.parse_reply("Re: x [#d6aae4]", body))

    def test_note_captured(self):
        reply = fb.parse_reply("x [#abcdef]", "错，这个接口已经下线了")
        self.assertIn("下线", reply.note)


class TestExtractEmail(unittest.TestCase):
    def _build(self, subject: str, body: str, *, html: bool = False) -> bytes:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = "someone@example.com"
        message["To"] = "me@example.com"
        if html:
            message.set_content(body, subtype="html")
        else:
            message.set_content(body)
        return message.as_bytes()

    def test_plain_text(self):
        subject, body = fb.extract_subject_and_body(self._build("标题 [#abc123]", "对"))
        self.assertIn("#abc123", subject)
        self.assertIn("对", body)

    def test_encoded_chinese_subject_is_decoded(self):
        """RFC 2047 编码的中文主题不解码就找不到标记。"""
        raw = self._build("中文标题 [#abc123]", "对")
        subject, _ = fb.extract_subject_and_body(raw)
        self.assertIn("中文标题", subject)
        self.assertIn("#abc123", subject)

    def test_html_only_falls_back(self):
        raw = self._build("<p>错</p>", "错", html=True)
        _, body = fb.extract_subject_and_body(raw)
        self.assertIn("错", body)

    def test_end_to_end_from_raw_message(self):
        raw = self._build("Re: 告警 [#d6aae4]", "不对")
        subject, body = fb.extract_subject_and_body(raw)
        reply = fb.parse_reply(subject, body)
        self.assertEqual(reply.vote, "down")


class TestFeedbackStore(unittest.TestCase):
    def setUp(self):
        if TMP.exists():
            shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        self.conn = store.connect(TMP / "data" / "ops.db")
        store.start_run(self.conn, "20261005T184422Z-api_watch-d6aae4", "api_watch",
                        "2026-10-05T18:44:22Z")
        store.finish_run(self.conn, "20261005T184422Z-api_watch-d6aae4",
                         finished_at="2026-10-05T18:44:30Z", status="degraded")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(TMP, ignore_errors=True)

    def test_record_and_stats(self):
        store.record_feedback(self.conn, run_id="20261005T184422Z-api_watch-d6aae4", vote="down")
        stats = store.feedback_stats(self.conn)
        self.assertEqual(stats["down"], 1)
        self.assertEqual(stats["accuracy"], 0.0)

    def test_repeat_feedback_overwrites(self):
        """人改主意是正常的 —— 同一来源对同一轮以最后一次为准。"""
        store.record_feedback(self.conn, run_id="20261005T184422Z-api_watch-d6aae4", vote="up")
        store.record_feedback(self.conn, run_id="20261005T184422Z-api_watch-d6aae4", vote="down")
        stats = store.feedback_stats(self.conn)
        self.assertEqual((stats["up"], stats["down"]), (0, 1))

    def test_different_sources_are_kept_separately(self):
        store.record_feedback(self.conn, run_id="20261005T184422Z-api_watch-d6aae4",
                              vote="up", source="cli")
        store.record_feedback(self.conn, run_id="20261005T184422Z-api_watch-d6aae4",
                              vote="down", source="email")
        self.assertEqual(len(store.feedback_for_run(self.conn,
                                                    "20261005T184422Z-api_watch-d6aae4")), 2)

    def test_bad_vote_rejected(self):
        with self.assertRaises(ValueError):
            store.record_feedback(self.conn, run_id="x", vote="maybe")

    def test_accuracy_is_none_without_data(self):
        """没有数据时说"没有"，不要说 0% 或 100%。"""
        self.assertIsNone(store.feedback_stats(self.conn)["accuracy"])

    def test_apply_reply_matches_run(self):
        reply = fb.ParsedReply(token="d6aae4", vote="down", note="不对")
        run_id = fb.apply_reply(store, self.conn, reply)
        self.assertEqual(run_id, "20261005T184422Z-api_watch-d6aae4")
        self.assertEqual(store.feedback_stats(self.conn)["down"], 1)

    def test_apply_reply_unknown_token_returns_none(self):
        reply = fb.ParsedReply(token="ffffff", vote="up", note="对")
        self.assertIsNone(fb.apply_reply(store, self.conn, reply))

    def test_find_run_ignores_eval_runs(self):
        """评测产生的 run 不是真实告警，不该被认领。"""
        store.start_run(self.conn, "eval-aw_02-abcdef", "api_watch", "2026-10-05T19:00:00Z")
        self.assertIsNone(fb.find_run_by_token(self.conn, "abcdef"))
        self.assertEqual(fb.find_run_by_token(self.conn, "d6aae4"),
                         "20261005T184422Z-api_watch-d6aae4")

    def test_find_run_rejects_non_hex_token(self):
        self.assertIsNone(fb.find_run_by_token(self.conn, "%"))


class TestFeedbackHint(unittest.TestCase):
    def test_hint_tells_how_to_reply(self):
        text = fb.feedback_hint("20261005T184422Z-api_watch-d6aae4")
        self.assertIn("回复", text)
        self.assertIn("#d6aae4", text)


class TestScanMarksOnlyClaimed(unittest.TestCase):
    """收信任务**只能标记自己认领的那几封**为已读。

    这条是真实 bug 换来的：第一版把整个未读列表都标记为已读 ——
    那会把用户收件箱里所有未读邮件（包括私人邮件）一起标成已读。
    这类副作用比"功能没生效"严重得多，而且不会有任何报错提示。
    """

    def setUp(self):
        from ops.tasks import feedback_scan

        self.module = feedback_scan
        if TMP.exists():
            shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "data").mkdir(parents=True, exist_ok=True)
        conn = store.connect(TMP / "data" / "ops.db")
        store.start_run(conn, "20261005T184422Z-api_watch-d6aae4", "api_watch",
                        "2026-10-05T18:44:22Z")
        store.finish_run(conn, "20261005T184422Z-api_watch-d6aae4",
                         finished_at="2026-10-05T18:44:30Z", status="degraded")
        conn.close()

        self.marked: list[bytes] = []

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def _fake_client(self, messages: dict[bytes, bytes]):
        marked = self.marked

        class FakeIMAP:
            def __init__(self, host, port):
                self.host, self.port = host, port

            def login(self, user, password):
                return "OK", [b""]

            def select(self, mailbox, readonly=False):
                return "OK", [b"1"]

            def search(self, charset, criteria):
                return "OK", [b" ".join(messages.keys())]

            def fetch(self, msg_id, spec):
                return "OK", [(b"meta", messages[msg_id])]

            def store(self, msg_id, op, flag):
                marked.append(msg_id)
                return "OK", [b""]

            def logout(self):
                return "BYE", [b""]

        return FakeIMAP

    def _msg(self, subject: str, body: str) -> bytes:
        message = EmailMessage()
        message["Subject"] = subject
        message.set_content(body)
        return message.as_bytes()

    def _run(self, messages, monkeypatch_mark_seen=True):
        import imaplib

        cfg = {
            "project": {"root": str(TMP)},
            "feedback": {"enabled": True, "imap_host": "x", "imap_port": 993,
                         "mailbox": "INBOX", "max_messages": 20,
                         "mark_seen": monkeypatch_mark_seen,
                         "user_env": "U", "pass_env": "P"},
            "_secrets": {"U": "u", "P": "p"},
        }
        original = imaplib.IMAP4_SSL
        imaplib.IMAP4_SSL = self._fake_client(messages)
        try:
            return self.module.scan_once(cfg)
        finally:
            imaplib.IMAP4_SSL = original

    def test_only_claimed_messages_are_marked_seen(self):
        messages = {
            b"1": self._msg("Re: 告警 [#d6aae4]", "不对"),      # 能认领
            b"2": self._msg("私人的邮件", "晚上一起吃饭"),        # 不该被碰
            b"3": self._msg("Re: 告警 [#ffffff]", "对"),        # 认不出 run_id
        }
        result = self._run(messages)
        self.assertEqual(result["applied"][0]["run_id"], "20261005T184422Z-api_watch-d6aae4")
        self.assertEqual(self.marked, [b"1"], "只该标记成功认领的那一封")

    def test_nothing_claimed_marks_nothing(self):
        messages = {b"7": self._msg("广告", "买点什么吧")}
        self._run(messages)
        self.assertEqual(self.marked, [])

    def test_dry_run_marks_nothing(self):
        messages = {b"1": self._msg("Re: 告警 [#d6aae4]", "对")}
        result = self._run(messages, monkeypatch_mark_seen=False)
        self.assertEqual(self.marked, [])
        self.assertEqual(len(result["applied"]), 1)

    def test_seen_count_reports_claimed_not_all(self):
        messages = {b"1": self._msg("Re: x [#d6aae4]", "对"), b"2": self._msg("无关", "嗯")}
        result = self._run(messages)
        self.assertEqual(result["seen"], 2)
        self.assertEqual(result["marked_seen"], 1)
        self.assertEqual(result["unparsed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
