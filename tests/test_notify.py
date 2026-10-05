"""告警通道的单元测试。

这里锁住的是一个**极易漏掉**的坑：企业微信群机器人即使 key 无效也返回 HTTP 200，
真正的结果在响应体的 `errcode` 里。只看 HTTP 状态码的话，
webhook 配错了也会显示"发送成功" —— 那是最阴险的静默失败。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import notify  # noqa: E402

TMP = Path(__file__).resolve().parents[1] / "data" / "_test_tmp" / "notify"


def make_cfg(**extra) -> dict:
    cfg = {
        "project": {"root": str(TMP).replace("\\", "/")},
        "alert": {"primary": "wecom", "wecom": {"webhook_env": "OPS_TEST_HOOK"}},
        "_secrets": {},
    }
    cfg.update(extra)
    return cfg


class NotifyTestBase(unittest.TestCase):
    def setUp(self):
        if TMP.exists():
            shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)


class TestWecomErrcode(NotifyTestBase):
    def _notifier(self) -> notify.Notifier:
        cfg = make_cfg()
        cfg["_secrets"] = {"OPS_TEST_HOOK": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake"}
        return notify.Notifier(cfg)

    def test_errcode_zero_is_sent(self):
        notifier = self._notifier()
        with mock.patch.object(
            notify, "post_json", return_value=(True, "200 ok", {"errcode": 0, "errmsg": "ok"})
        ):
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "sent")

    def test_errcode_nonzero_is_failure(self):
        """HTTP 200 但 errcode 非 0 —— 必须判为失败。"""
        notifier = self._notifier()
        with mock.patch.object(
            notify, "post_json",
            return_value=(True, "200 body", {"errcode": 93000, "errmsg": "invalid webhook url"}),
        ):
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "failed")
        self.assertIn("93000", result.detail)

    def test_non_json_response_is_failure(self):
        notifier = self._notifier()
        with mock.patch.object(notify, "post_json", return_value=(True, "200 <html>", None)):
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "failed")

    def test_http_error_is_failure(self):
        notifier = self._notifier()
        with mock.patch.object(notify, "post_json", return_value=(False, "HTTP 502", None)):
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "failed")
        self.assertIn("502", result.detail)


class TestSecretResolution(NotifyTestBase):
    def test_missing_secret_is_dry_run_not_failure(self):
        """没配凭据应该报 dry-run（链路是通的，只是没通道），而不是 failed。"""
        cfg = make_cfg()
        notifier = notify.Notifier(cfg)
        result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "dry-run")
        self.assertIn("OPS_TEST_HOOK", result.detail)

    def test_env_var_takes_priority_over_file(self):
        cfg = make_cfg()
        cfg["_secrets"] = {"OPS_TEST_HOOK": "from-file"}
        with mock.patch.dict("os.environ", {"OPS_TEST_HOOK": "from-env"}):
            self.assertEqual(cfgmod.resolve_secret(cfg, "OPS_TEST_HOOK"), "from-env")

    def test_file_fallback_used_when_env_absent(self):
        cfg = make_cfg()
        cfg["_secrets"] = {"OPS_TEST_HOOK": "from-file"}
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPS_TEST_HOOK", None)
            self.assertEqual(cfgmod.resolve_secret(cfg, "OPS_TEST_HOOK"), "from-file")

    def test_secret_sources_reports_where_it_came_from(self):
        cfg = make_cfg()
        cfg["_secrets"] = {"OPS_TEST_HOOK": "from-file"}
        import os

        os.environ.pop("OPS_TEST_HOOK", None)
        self.assertIn(cfgmod.SECRETS_FILENAME, cfgmod.secret_sources(cfg, "OPS_TEST_HOOK"))


class TestLocalSecretsFile(NotifyTestBase):
    """本地密钥文件必须能被真正读到，且容忍记事本保存出来的 BOM。"""

    def _write(self, text: str, bom: bool = False) -> Path:
        (TMP / "config").mkdir(parents=True, exist_ok=True)
        path = TMP / "config" / "config.toml"
        path.write_text('[project]\nroot = ""\n', encoding="utf-8")
        secret = TMP / "config" / cfgmod.SECRETS_FILENAME
        raw = text.encode("utf-8")
        if bom:
            raw = b"\xef\xbb\xbf" + raw
        secret.write_bytes(raw)
        return path

    def test_reads_plain_file(self):
        path = self._write('OPS_WECOM_WEBHOOK = "https://example.invalid/hook"\n')
        cfg = cfgmod.load_config(path)
        self.assertEqual(
            cfgmod.resolve_secret(cfg, "OPS_WECOM_WEBHOOK"), "https://example.invalid/hook"
        )

    def test_tolerates_bom(self):
        path = self._write('OPS_WECOM_WEBHOOK = "https://example.invalid/hook"\n', bom=True)
        cfg = cfgmod.load_config(path)
        self.assertEqual(
            cfgmod.resolve_secret(cfg, "OPS_WECOM_WEBHOOK"), "https://example.invalid/hook"
        )

    def test_config_with_bom_is_tolerated(self):
        """用户用记事本改过配置也不能崩。"""
        (TMP / "config").mkdir(parents=True, exist_ok=True)
        path = TMP / "config" / "config.toml"
        path.write_bytes(b'\xef\xbb\xbf[project]\nroot = ""\n')
        cfg = cfgmod.load_config(path)
        self.assertEqual(Path(cfg["project"]["root"]), TMP)


class TestWecomApp(NotifyTestBase):
    """自建应用通道：单人企业唯一可用的企业微信推送方式。"""

    def _cfg(self, **values) -> dict:
        cfg = make_cfg()
        cfg["alert"]["wecom_app"] = {
            "corpid_env": "OPS_TEST_CORPID",
            "secret_env": "OPS_TEST_SECRET",
            "agentid_env": "OPS_TEST_AGENTID",
            "touser": "@all",
        }
        cfg["_secrets"] = values
        return cfg

    def _ok_token(self, method, url, **kwargs):
        return SimpleNamespace(
            error=None, text="",
            body={"errcode": 0, "errmsg": "ok", "access_token": "TOK", "expires_in": 7200},
        )

    def test_missing_credentials_is_dry_run(self):
        notifier = notify.Notifier(self._cfg())
        result = notifier._send_wecom_app("m")
        self.assertEqual(result.status, "dry-run")
        self.assertIn("OPS_TEST_CORPID", result.detail)

    def test_agentid_must_be_numeric(self):
        cfg = self._cfg(OPS_TEST_CORPID="ww1", OPS_TEST_SECRET="s", OPS_TEST_AGENTID="abc")
        notifier = notify.Notifier(cfg)
        with mock.patch.object(notify, "request_json", side_effect=self._ok_token):
            with self.assertRaises(ValueError):
                notifier._send_wecom_app("m")

    def test_success_path_sends_expected_payload(self):
        cfg = self._cfg(OPS_TEST_CORPID="ww123456", OPS_TEST_SECRET="sec",
                        OPS_TEST_AGENTID="1000002")
        notifier = notify.Notifier(cfg)
        with mock.patch.object(notify, "request_json", side_effect=self._ok_token), \
             mock.patch.object(
                 notify, "post_json",
                 return_value=(True, "200 ok", {"errcode": 0, "errmsg": "ok"}),
             ) as post:
            result = notifier._send_wecom_app("hello")
        self.assertEqual(result.status, "sent")
        url, payload = post.call_args[0][0], post.call_args[0][1]
        self.assertIn("access_token=TOK", url)
        self.assertIn("/message/send", url)
        self.assertEqual(payload["touser"], "@all")
        self.assertEqual(payload["agentid"], 1000002)
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertEqual(payload["markdown"]["content"], "hello")

    def test_token_is_cached_across_calls(self):
        """token 有配额限制且两小时才过期，不该每轮都去换新的。"""
        cfg = self._cfg(OPS_TEST_CORPID="ww123456", OPS_TEST_SECRET="sec",
                        OPS_TEST_AGENTID="1000002")
        notifier = notify.Notifier(cfg)
        with mock.patch.object(notify, "request_json", side_effect=self._ok_token) as get_token, \
             mock.patch.object(notify, "post_json",
                               return_value=(True, "200 ok", {"errcode": 0, "errmsg": "ok"})):
            notifier._send_wecom_app("first")
            notifier._send_wecom_app("second")
        self.assertEqual(get_token.call_count, 1, "第二次应复用缓存的 token")

    def test_token_error_is_failure_with_hint(self):
        cfg = self._cfg(OPS_TEST_CORPID="ww-bad", OPS_TEST_SECRET="bad",
                        OPS_TEST_AGENTID="1000002")
        notifier = notify.Notifier(cfg)
        with mock.patch.object(
            notify, "request_json",
            return_value=SimpleNamespace(
                error=None, text="",
                body={"errcode": 40013, "errmsg": "invalid corpid"},
            ),
        ):
            result = notifier._send_wecom_app("m")
        self.assertEqual(result.status, "failed")
        self.assertIn("40013", result.detail)

    def test_errcode_nonzero_on_send_is_failure(self):
        cfg = self._cfg(OPS_TEST_CORPID="ww123456", OPS_TEST_SECRET="sec",
                        OPS_TEST_AGENTID="1000002")
        notifier = notify.Notifier(cfg)
        with mock.patch.object(notify, "request_json", side_effect=self._ok_token), \
             mock.patch.object(
                 notify, "post_json",
                 return_value=(True, "200 body", {"errcode": 60020, "errmsg": "not allow to access"}),
             ):
            result = notifier._send_wecom_app("m")
        self.assertEqual(result.status, "failed")
        self.assertIn("60020", result.detail)

    def test_invaliduser_is_failure_even_when_errcode_zero(self):
        """errcode=0 但没有有效接收人 —— 只按 errcode 判断会漏掉这种情况。"""
        cfg = self._cfg(OPS_TEST_CORPID="ww123456", OPS_TEST_SECRET="sec",
                        OPS_TEST_AGENTID="1000002")
        notifier = notify.Notifier(cfg)
        with mock.patch.object(notify, "request_json", side_effect=self._ok_token), \
             mock.patch.object(
                 notify, "post_json",
                 return_value=(True, "200 ok", {"errcode": 0, "errmsg": "ok",
                                                "invaliduser": "Zhangsan"}),
             ):
            result = notifier._send_wecom_app("m")
        self.assertEqual(result.status, "failed")
        self.assertIn("可见范围", result.detail)


class TestFallbackChain(NotifyTestBase):
    def test_primary_sent_short_circuits(self):
        cfg = make_cfg()
        cfg["alert"]["fallback"] = ["email"]
        cfg["_secrets"] = {"OPS_TEST_HOOK": "https://example.invalid/hook"}
        notifier = notify.Notifier(cfg)
        with mock.patch.object(
            notify, "post_json", return_value=(True, "200 ok", {"errcode": 0, "errmsg": "ok"})
        ) as post:
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "sent")
        self.assertEqual(post.call_count, 1)

    def test_falls_back_when_primary_fails(self):
        cfg = make_cfg()
        cfg["alert"]["fallback"] = ["email"]
        cfg["_secrets"] = {"OPS_TEST_HOOK": "https://example.invalid/hook"}
        cfg["alert"]["email"] = {"enabled": True, "user_env": "U", "pass_env": "P", "to_env": "T"}
        cfg["_secrets"].update({"U": "a@b.c", "P": "pw", "T": "a@b.c"})
        notifier = notify.Notifier(cfg)
        with mock.patch.object(
            notify, "post_json", return_value=(True, "200 body", {"errcode": 93000, "errmsg": "bad"})
        ), mock.patch.object(
            notifier, "_send_email", return_value=notify.SendResult("email", "sent", "ok")
        ):
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.channel, "email")
        self.assertIn("备用通道", result.detail)

    def test_all_unconfigured_reports_dry_run(self):
        """一个通道都没配 → dry-run（链路没试过），而不是 failed。"""
        cfg = make_cfg()
        cfg["alert"]["fallback"] = ["email"]
        notifier = notify.Notifier(cfg)
        result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "dry-run")

    def test_all_failed_is_reported_as_failed_with_every_channel(self):
        cfg = make_cfg()
        cfg["alert"]["fallback"] = ["email"]
        cfg["_secrets"] = {"OPS_TEST_HOOK": "https://example.invalid/hook"}
        notifier = notify.Notifier(cfg)
        with mock.patch.object(notify, "post_json", return_value=(False, "HTTP 502", None)), \
             mock.patch.object(
                 notifier, "_send_email",
                 return_value=notify.SendResult("email", "failed", "SMTP 挂了"),
             ):
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "failed")
        self.assertIn("wecom", result.detail)
        self.assertIn("email", result.detail)

    def test_cli_dry_run_never_sends(self):
        cfg = make_cfg()
        cfg["_secrets"] = {"OPS_TEST_HOOK": "https://example.invalid/hook"}
        notifier = notify.Notifier(cfg, dry_run=True)
        with mock.patch.object(notify, "post_json") as post:
            result = notifier.send(subject="s", markdown="m")
        self.assertEqual(result.status, "dry-run")
        post.assert_not_called()


class TestNtfy(NotifyTestBase):
    """ntfy 是"没有域名、没有公网 IP"时最省事的推送通道。"""

    def _cfg(self, url: str | None = None) -> dict:
        cfg = make_cfg()
        cfg["alert"]["ntfy"] = {"url_env": "OPS_TEST_NTFY", "title": "ops-pipeline",
                                "priority": "default", "tags": "warning"}
        cfg["_secrets"] = {"OPS_TEST_NTFY": url} if url else {}
        return cfg

    def test_missing_url_is_dry_run(self):
        result = notify.Notifier(self._cfg())._send_ntfy("m")
        self.assertEqual(result.status, "dry-run")
        self.assertIn("OPS_TEST_NTFY", result.detail)

    def test_success_sends_markdown_as_body(self):
        notifier = notify.Notifier(self._cfg("https://ntfy.sh/ops-test-topic"))
        with mock.patch.object(
            notify, "post_text", return_value=(True, "200 ok", {"id": "abc"})
        ) as post:
            result = notifier._send_ntfy("**ALERT** 出问题了")
        self.assertEqual(result.status, "sent")
        url, body = post.call_args[0][0], post.call_args[0][1]
        self.assertEqual(url, "https://ntfy.sh/ops-test-topic")
        self.assertIn("出问题了", body)          # 正文（含中文）走 body
        headers = post.call_args[1]["headers"]
        self.assertEqual(headers["Title"], "ops-pipeline")  # 标题走 ASCII，避免头编码问题

    def test_http_error_with_code_gives_hint(self):
        notifier = notify.Notifier(self._cfg("https://ntfy.sh/reserved"))
        with mock.patch.object(
            notify, "post_text",
            return_value=(False, "HTTP 403", {"code": 40302, "error": "topic not allowed"}),
        ):
            result = notifier._send_ntfy("m")
        self.assertEqual(result.status, "failed")
        self.assertIn("随机话题", result.detail)

    def test_long_body_is_truncated(self):
        notifier = notify.Notifier(self._cfg("https://ntfy.sh/t"))
        with mock.patch.object(
            notify, "post_text", return_value=(True, "200 ok", {})
        ) as post:
            notifier._send_ntfy("啊" * 5000)
        self.assertLessEqual(len(post.call_args[0][1].encode("utf-8")), notify.NTFY_LIMIT)


class TestPushPlus(NotifyTestBase):
    """PushPlus：微信扫码就能收推送，国内安卓装不了 ntfy 时的正解。"""

    def _cfg(self, token: str | None = None) -> dict:
        cfg = make_cfg()
        cfg["alert"]["pushplus"] = {"token_env": "OPS_TEST_PP", "template": "markdown"}
        cfg["_secrets"] = {"OPS_TEST_PP": token} if token else {}
        return cfg

    def test_missing_token_is_dry_run(self):
        result = notify.Notifier(self._cfg())._send_pushplus("s", "m")
        self.assertEqual(result.status, "dry-run")
        self.assertIn("OPS_TEST_PP", result.detail)

    def test_success_sends_expected_payload(self):
        notifier = notify.Notifier(self._cfg("tok123"))
        with mock.patch.object(
            notify, "post_json",
            return_value=(True, "200 ok", {"code": 200, "msg": "请求成功"}),
        ) as post:
            result = notifier._send_pushplus("[alert] 出问题了", "**ALERT** 正文")
        self.assertEqual(result.status, "sent")
        url, payload = post.call_args[0][0], post.call_args[0][1]
        self.assertIn("pushplus.plus", url)
        self.assertEqual(payload["token"], "tok123")
        self.assertEqual(payload["title"], "[alert] 出问题了")
        self.assertEqual(payload["content"], "**ALERT** 正文")
        self.assertEqual(payload["template"], "markdown")

    def test_non_200_code_is_failure_with_hint(self):
        """903 是实测拿到的错误码（假 token 真实调用 PushPlus 得到的）。"""
        notifier = notify.Notifier(self._cfg("bad"))
        with mock.patch.object(
            notify, "post_json",
            return_value=(True, "200 body",
                          {"code": 903, "data": "无效的用户token", "msg": "用户令牌不正确"}),
        ):
            result = notifier._send_pushplus("s", "m")
        self.assertEqual(result.status, "failed")
        self.assertIn("903", result.detail)
        self.assertIn("重新扫码", result.detail)

    def test_unknown_code_still_shows_server_message(self):
        """未收录的错误码也必须把服务端的中文说明带出来，不能只剩一个数字。"""
        notifier = notify.Notifier(self._cfg("tok"))
        with mock.patch.object(
            notify, "post_json",
            return_value=(True, "200 body", {"code": 12345, "msg": "今日发送条数已达上限"}),
        ):
            result = notifier._send_pushplus("s", "m")
        self.assertEqual(result.status, "failed")
        self.assertIn("12345", result.detail)
        self.assertIn("今日发送条数已达上限", result.detail)

    def test_http_error_is_failure(self):
        notifier = notify.Notifier(self._cfg("tok"))
        with mock.patch.object(notify, "post_json", return_value=(False, "HTTP 502", None)):
            result = notifier._send_pushplus("s", "m")
        self.assertEqual(result.status, "failed")


class TestSmtpLogin(NotifyTestBase):
    """SMTP 登录机制的选择。

    这是在 QQ 邮箱上实际踩到的坑：smtplib 默认优先 AUTH PLAIN，
    而 QQ 对 PLAIN 直接掐断连接，于是真正的 535 被吞成
    "Connection unexpectedly closed" —— 排查方向被彻底带偏。
    """

    class FakeServer:
        def __init__(self, mechanisms: str, password_reply=(535, b"Login fail")):
            self.esmtp_features = {"auth": mechanisms}
            self.password_reply = password_reply
            self.commands: list[tuple[str, str | None]] = []
            self.login_called = False

        def docmd(self, cmd, args=None):
            self.commands.append((cmd, args))
            if cmd == "AUTH":
                return 334, b"VXNlcm5hbWU6"
            if len(self.commands) == 2:
                return 334, b"UGFzc3dvcmQ6"
            return self.password_reply

        def login(self, user, password):
            self.login_called = True
            raise AssertionError("不该走到 smtplib 的 login()")

    def test_prefers_auth_login_when_available(self):
        server = self.FakeServer("LOGIN PLAIN", password_reply=(235, b"Authentication successful"))
        notify.smtp_login(server, "u@qq.com", "x" * 16)
        self.assertEqual(server.commands[0], ("AUTH", "LOGIN"))
        self.assertFalse(server.login_called)

    def test_raises_smtp_auth_error_with_server_text(self):
        """认证失败必须抛出带服务端原文的异常，而不是一个笼统的连接错误。"""
        server = self.FakeServer("LOGIN PLAIN", password_reply=(535, b"Login fail. Account is abnormal"))
        with self.assertRaises(notify.smtplib.SMTPAuthenticationError) as ctx:
            notify.smtp_login(server, "u@qq.com", "wrong")
        self.assertEqual(ctx.exception.smtp_code, 535)
        self.assertIn("Account is abnormal", ctx.exception.smtp_error.decode())

    def test_falls_back_when_login_not_advertised(self):
        server = self.FakeServer("PLAIN", password_reply=(235, b"ok"))
        server.login = lambda user, password: setattr(server, "login_called", True)
        notify.smtp_login(server, "u@x.com", "p")
        self.assertTrue(server.login_called)


class TestAlertRender(unittest.TestCase):
    """告警邮件必须"人能读"。

    这是从真实反馈来的：邮件里曾经塞满 `samples:` / `baseline.worldtime:` /
    `modal_body_hash` 这类字段，收件人看到的是一堵墙。现在只放三块人话。
    """

    def _render(self, **overrides):
        args = dict(
            headline="worldtime 连不上了",
            severity="alert",
            plain_summary="外部接口 worldtime 从今天凌晨起一直连不上，已连续失败 5 次；另外两个正常。",
            impact="看起来是对方服务的问题，不影响你自己这边的运行。",
            action="先不用管；明天还是连不上再去看看那个接口。",
            evidence=["samples: worldtime ok=0, status_code=null", "baseline.worldtime: p95_ms=0.0"],
            metrics={"targets": 3, "ok": 2, "failed": 1, "p95_ms": 1120.5, "anomalies": 1},
            report_path=r"D:\reports\api_watch-1.md",
        )
        args.update(overrides)
        return notify.render_markdown(**args)

    def test_contains_plain_three_parts(self):
        text = self._render()
        self.assertIn("worldtime 连不上了", text)
        self.assertIn("一直连不上", text)
        self.assertIn("**这意味着**", text)
        self.assertIn("**建议**", text)

    def test_does_not_leak_machine_fields(self):
        """核心断言：机器字段绝不能出现在邮件里。"""
        text = self._render()
        for junk in ("samples:", "baseline.", "modal_body_hash", "p95", "status_code"):
            self.assertNotIn(junk, text, f"邮件里不该出现 {junk!r}")

    def test_severity_label_is_chinese(self):
        self.assertIn("需要注意", self._render())
        self.assertIn("严重", self._render(severity="critical"))
        self.assertNotIn("ALERT", self._render())

    def test_metrics_footer_is_plain(self):
        self.assertIn("目标 2/3 正常", self._render())
        self.assertIn("失败 1", self._render())

    def test_falls_back_to_evidence_when_no_plain_summary(self):
        """判断层只给了 evidence 时，也不能发一封空邮件。"""
        text = self._render(plain_summary=None, impact=None, action=None)
        self.assertIn("初步证据", text)
        self.assertIn("worldtime ok=0", text)

    def test_evidence_omitted_when_plain_summary_present(self):
        """有 plain_summary 时，evidence 不该再出现（它属于报告文件）。"""
        self.assertNotIn("初步证据", self._render())

    def test_degraded_marker(self):
        self.assertIn("规则层", self._render(ai_degraded=True))

    def test_report_path_included(self):
        self.assertIn("api_watch-1.md", self._render())

    def test_no_metrics_no_crash(self):
        self.assertIn("worldtime", self._render(metrics=None))


class TestRuleVerdictPlainText(unittest.TestCase):
    """降级≠难读：规则层兜底时也要产出人话。"""

    def _analysis(self, *anomalies):
        from ops.core import judge

        return judge.Analysis(
            metrics={"targets": 3, "ok": 2, "failed": 1},
            samples=[], baseline={}, anomalies=list(anomalies),
        )

    def _anomaly(self, kind, target="worldtime", severity_hint="alert"):
        from ops.core import judge

        return judge.Anomaly(kind=kind, target=target, detail="技术细节", severity_hint=severity_hint)

    def test_healthy_text(self):
        from ops.core import judge

        happened, _impact, action = judge.plain_text_for(self._analysis())
        self.assertIn("一切正常", happened)
        self.assertIn("无需处理", action)

    def test_anomaly_translated_to_plain_words(self):
        from ops.core import judge

        happened, _impact, _action = judge.plain_text_for(
            self._analysis(self._anomaly("consecutive_failure"))
        )
        self.assertIn("连续失败", happened)
        self.assertIn("worldtime", happened)
        self.assertNotIn("consecutive_failure", happened)

    def test_unknown_kind_does_not_crash(self):
        from ops.core import judge

        happened, _, _ = judge.plain_text_for(self._analysis(self._anomaly("brand_new_kind")))
        self.assertIn("发现异常", happened)

    def test_rule_verdict_includes_plain_fields(self):
        from ops.core import judge

        verdict = judge.rule_verdict(
            "run-1", "api_watch", self._analysis(self._anomaly("consecutive_failure"))
        )
        for key in ("plain_summary", "impact", "suggested_action"):
            self.assertTrue(verdict.get(key), f"缺少 {key}")
        self.assertNotIn("consecutive_failure", verdict["plain_summary"])


class TestErrorHints(NotifyTestBase):
    """报错必须告诉人怎么办 —— 否则排查成本全落在使用者头上。"""

    def _cfg(self) -> dict:
        cfg = make_cfg()
        cfg["_secrets"] = {"OPS_TEST_HOOK": "https://example.invalid/hook"}
        return cfg

    def _send_with(self, body) -> notify.SendResult:
        notifier = notify.Notifier(self._cfg())
        with mock.patch.object(notify, "post_json", return_value=(True, "200 body", body)):
            return notifier.send(subject="s", markdown="m")

    def test_ip_whitelist_hint(self):
        """60020 是实际踩过的坑：自建应用要求配置「企业可信IP」。"""
        result = self._send_with({"errcode": 60020, "errmsg": "not allow to access from your ip"})
        self.assertEqual(result.status, "failed")
        self.assertIn("企业可信IP", result.detail)
        self.assertIn("IP 会变", result.detail)

    def test_bad_secret_hint(self):
        result = self._send_with({"errcode": 40001, "errmsg": "invalid credential"})
        self.assertIn("Secret", result.detail)

    def test_empty_visibility_hint(self):
        result = self._send_with({"errcode": 82001, "errmsg": "no visible user"})
        self.assertIn("可见范围", result.detail)

    def test_unknown_errcode_gives_lookup_link(self):
        result = self._send_with({"errcode": 123456, "errmsg": "whatever"})
        self.assertIn("devtool/query?e=123456", result.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
