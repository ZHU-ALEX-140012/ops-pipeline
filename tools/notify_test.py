"""告警通道测试：只验证"能不能把消息发出去"，不碰监控逻辑。

为什么单独做一个工具：告警通道挂掉是**最安静的故障** —— 监控照常跑、报告照常生成、
你以为"今天没异常"，其实是消息根本没发出去。所以配好通道后第一件事就是点一炮。

用法：
    python tools/notify_test.py                        # 用主配置的主通道
    python tools/notify_test.py --channel wecom_app    # 测企业微信自建应用
    python tools/notify_test.py --channel email        # 测邮件
    python tools/notify_test.py --config config/config.toml

退出码：0 = 真的发出去了；1 = 没发出去（dry-run / 失败），并说明卡在哪一环。

注意：控制台输出只用 ASCII 标记（[OK] / [!!]）。中文控制台默认是 GBK 代码页，
     ✓ ✗ ⚠ 这类字符编码不了，会让本该打印的提示变成 traceback。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import notify  # noqa: E402
from ops.core import make_console_safe  # noqa: E402

CHANNELS = ("pushplus", "ntfy", "email", "wecom", "wecom_app")


def credential_lines(cfg: dict, channel: str) -> list[str]:
    """把该通道用到的每个凭据及其来源列出来 —— 排查时最想知道的就是这个。"""
    alert = cfg.get("alert") or {}
    if channel == "pushplus":
        pp = alert.get("pushplus") or {}
        return [
            f"token        : {cfgmod.secret_sources(cfg, pp.get('token_env'))}",
            f"template     : {pp.get('template', 'markdown')}",
        ]
    if channel == "ntfy":
        ntfy = alert.get("ntfy") or {}
        return [
            f"推送地址     : {cfgmod.secret_sources(cfg, ntfy.get('url_env'))}",
            f"priority     : {ntfy.get('priority', 'default')}",
        ]
    if channel == "wecom":
        env = (alert.get("wecom") or {}).get("webhook_env")
        return [f"webhook      : {cfgmod.secret_sources(cfg, env)}"]
    if channel == "wecom_app":
        app = alert.get("wecom_app") or {}
        return [
            f"CorpID       : {cfgmod.secret_sources(cfg, app.get('corpid_env'))}",
            f"Secret       : {cfgmod.secret_sources(cfg, app.get('secret_env'))}",
            f"AgentId      : {cfgmod.secret_sources(cfg, app.get('agentid_env'))}",
            f"touser       : {app.get('touser', '@all')}",
        ]
    email = alert.get("email") or {}
    return [
        f"enabled      : {email.get('enabled')}",
        f"SMTP 用户     : {cfgmod.secret_sources(cfg, email.get('user_env'))}",
        f"SMTP 密码     : {cfgmod.secret_sources(cfg, email.get('pass_env'))}",
    ]


def main() -> int:
    # 报警信息里可能带 emoji（服务端原样返回），GBK 控制台会编码失败。
    # 先把输出流设成"打不出来就替换"，否则报错信息本身会变成 traceback。
    make_console_safe()
    parser = argparse.ArgumentParser(description="测试告警通道是否真的通畅")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--channel", choices=CHANNELS, default=None,
                        help="默认用配置里的主通道")
    parser.add_argument("--headline", default=None, help="自定义消息标题（默认带时间戳）")
    args = parser.parse_args()

    try:
        cfg = cfgmod.load_config(args.config)
    except cfgmod.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    notifier = notify.Notifier(cfg)
    channel = args.channel or notifier.primary
    if channel not in CHANNELS:
        print(f"不支持的主通道 {channel!r}，可选：{', '.join(CHANNELS)}", file=sys.stderr)
        return 2

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    headline = args.headline or f"ops-pipeline 通道测试（{stamp}）"
    markdown = notify.render_markdown(
        headline=headline,
        severity="notice",
        evidence=[
            "这是一条**测试消息**，由 tools/notify_test.py 发出，不代表任何真实故障。",
            f"通道：`{channel}`",
            f"配置文件：{cfg.get('_config_path')}",
        ],
        metrics=None,
    )

    print(f"通道      : {channel}")
    for line in credential_lines(cfg, channel):
        print(f"凭据      : {line}" if line.startswith(("webhook", "enabled")) else f"            {line}")
    print("正在发送…")

    result = notifier._dispatch(channel, headline, markdown)  # 只测这一个通道，不走 fallback
    print(f"结果      : {result.status}")
    print(f"详情      : {result.detail or '（无）'}")

    if result.status == "sent":
        print("\n[OK] 消息已投递。去企业微信/邮箱确认一下有没有收到——本工具只能证明接口返回成功。")
        return 0
    if result.status == "dry-run":
        print(
            "\n[!!] 没有真的发送（dry-run）：这个通道还没配好。\n"
            f"  配置方式（按省事程度排序）：\n"
            f"    scripts\\set-secret.cmd -Name OPS_PUSHPLUS_TOKEN -Masked\n"
            f"        推荐：微信扫码登录 https://www.pushplus.plus/ 拿 token，消息直接推到微信，不用装 App\n"
            f"    scripts\\set-webhook.cmd        企业微信群机器人（需要普通内部群）\n"
            f"    scripts\\set-wecom-app.cmd     企业微信自建应用（需要已备案域名或公网服务器）\n"
            f"    也可以在环境变量里设置对应的键"
        )
        return 1
    print("\n[!!] 发送失败。常见原因见上方详情（凭据抄错 / 可见范围不含自己 / 网络不通）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
