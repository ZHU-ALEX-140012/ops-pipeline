"""日志模板提取的单元测试。

模板提取是整条日志流水线的地基：它把模板分错了，后面的"新模板检测"和
"量级突增检测"全都建立在错误的基线上。所以这里两侧都测。
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import logparse  # noqa: E402


class TestLevelExtraction(unittest.TestCase):
    def test_common_levels(self):
        for name in ("INFO", "WARN", "ERROR", "DEBUG", "FATAL"):
            self.assertEqual(logparse.extract_level(f"{name} something happened"), name)

    def test_warning_alias(self):
        self.assertEqual(logparse.extract_level("WARNING disk almost full"), "WARN")

    def test_critical_alias(self):
        self.assertEqual(logparse.extract_level("CRITICAL service down"), "FATAL")

    def test_lowercase(self):
        self.assertEqual(logparse.extract_level("error boom"), "ERROR")

    def test_unknown_when_absent(self):
        """取不到级别就 UNKNOWN，绝不猜。"""
        self.assertEqual(logparse.extract_level("just some text"), "UNKNOWN")

    def test_level_inside_message_not_confused_as_prefix(self):
        # 级别出现即可，不要求必须在行首（有些格式前面有线程名）
        self.assertEqual(logparse.extract_level("thread-1 ERROR boom"), "ERROR")


class TestTimestamp(unittest.TestCase):
    def test_millisecond_format(self):
        stamp, rest = logparse.parse_timestamp("2026-10-05 01:30:00.123 INFO hello")
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.microsecond, 123000)
        self.assertEqual(rest, "INFO hello")

    def test_second_format(self):
        stamp, rest = logparse.parse_timestamp("2026-10-05 01:30:00 INFO hello")
        self.assertIsNotNone(stamp)
        self.assertEqual(rest, "INFO hello")

    def test_utc_z_suffix(self):
        stamp, _ = logparse.parse_timestamp("2026-10-05T01:30:00Z INFO hello")
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.utcoffset(), timedelta(0))

    def test_explicit_offset(self):
        stamp, _ = logparse.parse_timestamp("2026-10-05T01:30:00+08:00 INFO hello")
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.utcoffset(), timedelta(hours=8))

    def test_bracketed(self):
        stamp, rest = logparse.parse_timestamp("[2026-10-05 01:30:00] INFO hello")
        self.assertIsNotNone(stamp)
        self.assertEqual(rest, "INFO hello")

    def test_slash_format(self):
        stamp, _ = logparse.parse_timestamp("2026/10/05 01:30:00 INFO hello")
        self.assertIsNotNone(stamp)

    def test_no_timestamp(self):
        stamp, rest = logparse.parse_timestamp("INFO hello")
        self.assertIsNone(stamp)
        self.assertEqual(rest, "INFO hello")

    def test_naive_timestamp_gets_local_tz(self):
        """无时区信息的日志按本地时区解释 —— 写日志的机器就是本机。"""
        stamp, _ = logparse.parse_timestamp("2026-10-05 01:30:00 INFO hello")
        self.assertIsNotNone(stamp.tzinfo)


class TestTokenNormalization(unittest.TestCase):
    def test_pure_number(self):
        self.assertEqual(logparse.normalize_token("12345"), "<*>")

    def test_key_value_with_number(self):
        self.assertEqual(logparse.normalize_token("ms=412"), "ms=<*>")

    def test_path_with_id(self):
        self.assertEqual(logparse.normalize_token("path=/api/users/1"), "path=/api/users/<*>")

    def test_ip(self):
        self.assertEqual(logparse.normalize_token("10.0.0.5"), "<*>")

    def test_uuid(self):
        self.assertEqual(
            logparse.normalize_token("550e8400-e29b-41d4-a716-446655440000"), "<*>"
        )

    def test_plain_word_unchanged(self):
        self.assertEqual(logparse.normalize_token("connection"), "connection")

    def test_empty(self):
        self.assertEqual(logparse.normalize_token(""), "")


class TestTokenize(unittest.TestCase):
    def test_level_comes_first(self):
        """级别必须进 token 序列：否则 INFO 与 ERROR 可能被并成同一模板。"""
        tokens = logparse.tokenize("[http] request completed ms=12", "ERROR")
        self.assertEqual(tokens[0], "ERROR")

    def test_normalizes_all_parts(self):
        tokens = logparse.tokenize("user 12345 from 10.0.0.9", "INFO")
        self.assertIn("<*>", tokens)
        self.assertNotIn("12345", tokens)


class TestTemplateMiner(unittest.TestCase):
    def test_similar_lines_merge(self):
        miner = logparse.TemplateMiner()
        first = miner.add(logparse.parse_line("2026-10-05 01:00:00 INFO user 111 logged in"))
        second = miner.add(logparse.parse_line("2026-10-05 01:00:01 INFO user 222 logged in"))
        self.assertEqual(first, second, "同类日志必须归到同一模板")
        self.assertEqual(miner.template_count, 1)

    def test_different_levels_do_not_merge(self):
        """INFO 与 ERROR 正文相同也必须分开，否则错误会被埋进正常流量。"""
        miner = logparse.TemplateMiner()
        info = miner.add(logparse.parse_line("2026-10-05 01:00:00 INFO db write ok"))
        error = miner.add(logparse.parse_line("2026-10-05 01:00:01 ERROR db write ok"))
        self.assertNotEqual(info, error)
        self.assertEqual(miner.template_count, 2)

    def test_different_token_counts_do_not_merge(self):
        miner = logparse.TemplateMiner()
        miner.add(logparse.parse_line("2026-10-05 01:00:00 INFO a b c d e"))
        miner.add(logparse.parse_line("2026-10-05 01:00:01 INFO a b c"))
        self.assertEqual(miner.template_count, 2)

    def test_variable_position_is_generalized(self):
        miner = logparse.TemplateMiner()
        miner.add(logparse.parse_line("2026-10-05 01:00:00 INFO query table=users rows=10 ms=5"))
        template = miner.add(logparse.parse_line("2026-10-05 01:00:01 INFO query table=orders rows=99 ms=7"))
        # table= 后面是词而非数字，不会被归一，所以这两行会各自成模板；
        # 但数字位置被泛化这件事要用同结构不同数字来验证：
        self.assertIn("rows=<*>", template)

    def test_totally_different_lines_stay_separate(self):
        miner = logparse.TemplateMiner()
        miner.add(logparse.parse_line("2026-10-05 01:00:00 INFO alpha beta gamma"))
        miner.add(logparse.parse_line("2026-10-05 01:00:01 INFO zeta eta theta"))
        self.assertEqual(miner.template_count, 2)

    def test_count_tracking(self):
        miner = logparse.TemplateMiner()
        for index in range(5):
            miner.add(logparse.parse_line(f"2026-10-05 01:00:0{index} INFO user {index} logged in"))
        self.assertEqual(miner.stats()["merged"], 4)

    def test_empty_line_is_safe(self):
        miner = logparse.TemplateMiner()
        parsed = logparse.parse_line("")
        self.assertEqual(miner.add(parsed), "")


class TestLevelIsNotDuplicated(unittest.TestCase):
    """级别词会作为模板的首 token，所以必须从正文里摘掉。

    不摘就会出现 "ERROR ERROR [db] ..." 这种模板，既难读，也多占一个 token 位，
    会让相似度计算失真。
    """

    def test_message_has_level_stripped(self):
        parsed = logparse.parse_line("2026-10-05 01:30:00 ERROR [db] connection failed")
        self.assertEqual(parsed.level, "ERROR")
        self.assertNotIn("ERROR", parsed.message)
        self.assertIn("[db]", parsed.message)

    def test_template_contains_level_once(self):
        miner = logparse.TemplateMiner()
        parsed = logparse.parse_line("2026-10-05 01:30:00 ERROR [db] connection failed")
        template = miner.add(parsed)
        self.assertEqual(template.count("ERROR"), 1)
        self.assertTrue(template.startswith("ERROR "))

    def test_warning_alias_is_normalized_and_stripped(self):
        miner = logparse.TemplateMiner()
        parsed = logparse.parse_line("2026-10-05 01:30:00 WARNING disk almost full")
        template = miner.add(parsed)
        self.assertTrue(template.startswith("WARN "))
        self.assertNotIn("WARNING", template)

    def test_level_in_middle_of_line(self):
        parsed = logparse.parse_line("2026-10-05 01:30:00 thread-7 ERROR boom happened")
        self.assertEqual(parsed.level, "ERROR")
        self.assertNotIn("ERROR", parsed.message)
        self.assertIn("thread-7", parsed.message)


class TestParseLine(unittest.TestCase):
    def test_full_line(self):
        parsed = logparse.parse_line(
            "2026-10-05 01:30:00.500 ERROR [db] connection failed host=10.0.0.5 port=5432 retry=1"
        )
        self.assertEqual(parsed.level, "ERROR")
        self.assertTrue(parsed.is_error)
        self.assertIsNotNone(parsed.ts)
        self.assertTrue(parsed.timestamp_matched)

    def test_template_is_stable_across_variables(self):
        one = logparse.parse_line("2026-10-05 01:30:00 INFO [http] request path=/api/users/1 ms=12")
        two = logparse.parse_line("2026-10-05 02:45:11 INFO [http] request path=/api/users/9999 ms=880")
        miner = logparse.TemplateMiner()
        self.assertEqual(miner.add(one), miner.add(two))


if __name__ == "__main__":
    unittest.main(verbosity=2)
