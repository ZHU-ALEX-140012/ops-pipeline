"""极简只读面板：一条命令看清这套东西现在什么状态。

    python tools/dashboard.py                 # 起在 127.0.0.1:8899
    python tools/dashboard.py --port 9000
    python tools/dashboard.py --json          # 不启服务，直接打印汇总 JSON
    python tools/dashboard.py --once          # 渲染一次 HTML 到 stdout（便于回归）

## 为什么是"极简 + 只读"

* **零前端依赖**：一个内联样式的 HTML 页，不用构建、不装 node_modules。
  项目一路坚持"零第三方依赖"，面板不该是第一个例外。
* **只绑 127.0.0.1**：它读的是你的运维数据（含告警详情），不该出现在局域网上。
* **只读**：所有查询走 `mode=ro` 连接，没有写接口、没有"操作"按钮 ——
  一个能触发动作的面板就是一个新的攻击面。
* **没有它也能用**：报告是 markdown、数据在 SQLite、告警在邮件里。
  面板只是把已经存在的东西摆到一起，不是新的真相来源。

## 它回答的问题

打开就该看到：**有东西坏了吗？** 而不是"欢迎使用运维平台"。
所以首屏第一块是"需要你注意的事"，空闲时它明确写"没有"。
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.core import config as cfgmod  # noqa: E402
from ops.core import doctor, store  # noqa: E402

DEFAULT_PORT = 8899
HOST = "127.0.0.1"


# ---------------------------------------------------------------- 数据汇总

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def collect_summary(cfg: dict[str, Any], *, hours: int = 24) -> dict[str, Any]:
    """把面板要显示的东西一次查完。**纯读**，不写任何东西。

    每个块都单独 try：面板最没用的失败方式是"因为它崩了，什么都看不到"。
    """
    now = datetime.now(timezone.utc)
    since = _iso(now - timedelta(hours=hours))
    out: dict[str, Any] = {"generated_at": _iso(now), "window_hours": hours, "blocks": {}}

    # 这两块**不依赖历史库**，所以放在最前面算：
    # 一个还没跑过任何任务的机器，也应该能看到自检结果和评测分数。
    out["blocks"]["eval"] = _latest_eval(cfg)
    out["blocks"]["doctor"] = _doctor_brief(cfg)

    db_path = cfgmod.data_dir(cfg, "ops.db")
    if not Path(db_path).is_file():
        out["blocks"]["error"] = f"历史库还不存在：{db_path}（跑过第一次就有了）"
        return out

    conn = store.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        out["blocks"]["tasks"] = _tasks(conn, cfg, now)
        out["blocks"]["unresolved"] = store.unresolved_alerts_all(conn)
        out["blocks"]["alerts"] = store.alerts_since(conn, hours=hours)
        out["blocks"]["runs"] = _recent_runs(conn, since)
        out["blocks"]["tokens"] = store.token_stats_since(conn, since)
        out["blocks"]["feedback"] = store.feedback_stats(conn)
        out["blocks"]["alert_stats"] = store.alert_stats_since(conn, since)
    except sqlite3.Error as exc:
        out["blocks"]["error"] = f"查询失败：{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return out


def _tasks(conn: sqlite3.Connection, cfg: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """每个任务最近一次运行 + 距今多久 vs 预期间隔。"""
    expect = (cfg.get("schedule") or {}).get("heartbeat_expect") or {}
    rows = conn.execute(
        "SELECT task, MAX(started_at) AS last_run, COUNT(*) AS runs "
        "FROM run WHERE id NOT LIKE 'eval-%' GROUP BY task ORDER BY task"
    ).fetchall()
    out = []
    for row in rows:
        task = str(row["task"])
        last = str(row["last_run"] or "")
        age_minutes = None
        try:
            when = datetime.fromisoformat(last.replace("Z", "+00:00"))
            age_minutes = round((now - when).total_seconds() / 60, 1)
        except ValueError:
            pass
        limit = expect.get(task)
        out.append({
            "task": task,
            "last_run": last,
            "age_minutes": age_minutes,
            "limit_minutes": limit,
            "stale": bool(limit and age_minutes is not None and age_minutes > float(limit)),
            "runs_in_db": int(row["runs"]),
        })
    return out


def _recent_runs(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, task, status, started_at, items_ok, items_failed, ai_status, "
        "tokens_in, tokens_out, substr(COALESCE(summary,''),1,140) AS summary "
        "FROM run WHERE started_at>=? AND id NOT LIKE 'eval-%' "
        "ORDER BY started_at DESC LIMIT 25",
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_eval(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """最近一次评测的分数 —— 面板上唯一"不是运行数据"的东西，
    但它回答了"判断层还准不准"，值得占一格。"""
    evals_dir = cfgmod.data_dir(cfg, "evals")
    if not Path(evals_dir).is_dir():
        return None
    files = sorted(Path(evals_dir).glob("*.json"))
    if not files:
        return None
    try:
        run = json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    metrics = run.get("metrics") or {}
    arms = metrics.get("arms") or {}
    return {
        "file": files[-1].name,
        "started_at": run.get("started_at"),
        "mode": run.get("mode"),
        "arms": [
            {"key": key, "label": next((a.get("label", key) for a in (run.get("arms") or [])
                                        if a.get("key") == key), key),
             "precision": (m.get("alert") or {}).get("precision"),
             "recall": (m.get("alert") or {}).get("recall"),
             "root_accuracy": m.get("root_accuracy"),
             "tokens_in": (m.get("tokens") or {}).get("in"),
             "avg_duration_s": m.get("avg_duration_s")}
            for key, m in arms.items() if m
        ],
    }


def _doctor_brief(cfg: dict[str, Any]) -> dict[str, Any]:
    """只跑**离线**那几项：面板刷新不该去连 SMTP/IMAP。"""
    try:
        checks = doctor.run_doctor(cfg, offline=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    counts = doctor.summarize(checks)
    return {
        "counts": counts,
        "problems": [{"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                     for c in checks if c.status in (doctor.WARN, doctor.FAIL)],
    }


# ---------------------------------------------------------------- 渲染

def _fmt_pct(value: Any) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ago(minutes: float | None) -> str:
    if minutes is None:
        return "未知"
    if minutes < 60:
        return f"{minutes:.0f} 分钟前"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f} 小时前"
    return f"{minutes / 1440:.1f} 天前"


CSS = """
:root { color-scheme: light dark; --fg:#1a1a1a; --muted:#666; --line:#e3e3e3;
        --ok:#0a7d32; --warn:#a86400; --bad:#b3261e; --bg:#fff; --card:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8e8; --muted:#9aa0a6; --line:#3a3a3a; --bg:#141414; --card:#1d1d1d; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:15px; margin:0 0 10px; color:var(--muted); font-weight:600;
     text-transform:none; letter-spacing:.02em; }
.meta { color:var(--muted); font-size:12px; margin-bottom:20px; }
.grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
.full { grid-column:1/-1; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; }
tr:last-child td { border-bottom:none; }
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.muted{color:var(--muted)} .mono{font-family:ui-monospace,Consolas,monospace; font-size:12px; }
.big { font-size:26px; font-weight:600; }
.kv { display:flex; gap:24px; flex-wrap:wrap; }
.kv div { min-width:110px; }
.kv .muted { font-size:12px; }
.empty { color:var(--ok); }
ul { margin:6px 0 0; padding-left:18px; }
li { margin:2px 0; }
footer { color:var(--muted); font-size:12px; margin-top:24px; }
"""


def render_html(summary: dict[str, Any]) -> str:
    blocks = summary.get("blocks") or {}
    esc = html.escape
    parts: list[str] = []
    add = parts.append

    add("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>")
    add("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    add("<title>ops-pipeline 状态</title>")
    add(f"<style>{CSS}</style></head><body>")
    add("<h1>ops-pipeline 状态</h1>")
    add(f"<div class='meta'>生成于 {esc(str(summary.get('generated_at')))}"
        f" · 窗口 {summary.get('window_hours')} 小时 · 只读</div>")

    if blocks.get("error"):
        add(f"<div class='card'><h2>读不到数据</h2><p class='bad'>{esc(blocks['error'])}</p></div>")
        add("</body></html>")
        return "\n".join(parts)

    add("<div class='grid'>")

    # ---------------------------------------------------------- 需要你注意的事（放第一）
    unresolved = blocks.get("unresolved") or []
    problems = ((blocks.get("doctor") or {}).get("problems")) or []
    add("<div class='card full'><h2>需要你注意的事</h2>")
    if not unresolved and not problems:
        add("<p class='empty'>没有。未恢复告警 0 条，自检无失败项。</p>")
    else:
        if unresolved:
            add(f"<p><b class='bad'>未恢复告警 {len(unresolved)} 条</b></p><ul>")
            for item in unresolved[:10]:
                add(f"<li><span class='mono'>{esc(str(item.get('task')))}"
                    f"/{esc(str(item.get('target')))}</span> "
                    f"[{esc(str(item.get('severity')))}] "
                    f"{esc(str(item.get('detail') or '')[:120])}</li>")
            add("</ul>")
        if problems:
            add(f"<p><b class='warn'>自检有 {len(problems)} 项要处理</b></p><ul>")
            for item in problems:
                add(f"<li><b>{esc(item['name'])}</b>：{esc(item['detail'])}")
                if item.get("fix"):
                    add(f"<br><span class='muted'>怎么办：{esc(item['fix'])}</span>")
                add("</li>")
            add("</ul>")
    add("</div>")

    # ---------------------------------------------------------- 任务健康度
    add("<div class='card'><h2>任务健康度</h2><table>")
    add("<tr><th>任务</th><th>最近运行</th><th>预期间隔</th></tr>")
    for task in blocks.get("tasks") or []:
        cls = "bad" if task.get("stale") else "ok"
        limit = task.get("limit_minutes")
        add(f"<tr><td class='mono'>{esc(task['task'])}</td>"
            f"<td class='{cls}'>{esc(_ago(task.get('age_minutes')))}"
            f"<div class='muted mono'>{esc(str(task.get('last_run')))}</div></td>"
            f"<td class='muted'>{'每 ' + str(limit) + ' 分钟' if limit else '未监视'}</td></tr>")
    if not blocks.get("tasks"):
        add("<tr><td colspan='3' class='muted'>还没有任何运行记录</td></tr>")
    add("</table></div>")

    # ---------------------------------------------------------- 成本
    tokens = blocks.get("tokens") or {}
    add("<div class='card'><h2>成本（窗口内）</h2>")
    add("<div class='kv'>")
    add(f"<div><div class='big'>{tokens.get('tokens_in', 0)}</div>"
        f"<div class='muted'>输入 tokens</div></div>")
    add(f"<div><div class='big'>{tokens.get('tokens_out', 0)}</div>"
        f"<div class='muted'>输出 tokens</div></div>")
    add(f"<div><div class='big'>{tokens.get('cache_read', 0)}</div>"
        f"<div class='muted'>cache 读</div></div>")
    add("</div>")
    add("<p class='muted' style='margin-top:12px'>cache 读的单价远低于普通输入，"
        "这里只报用量，不折算成钱。</p>")
    add("</div>")

    # ---------------------------------------------------------- 告警统计
    stats = {str(r["send_status"]): int(r["n"]) for r in blocks.get("alert_stats") or []}
    add("<div class='card'><h2>告警（窗口内）</h2><div class='kv'>")
    for key, label in (("sent", "已投递"), ("suppressed", "已抑制"),
                       ("dry-run", "未配通道"), ("failed", "发送失败")):
        cls = "bad" if (key == "failed" and stats.get(key)) else ""
        add(f"<div><div class='big {cls}'>{stats.get(key, 0)}</div>"
            f"<div class='muted'>{label}</div></div>")
    add("</div></div>")

    # ---------------------------------------------------------- 人工反馈
    fb = blocks.get("feedback") or {}
    add("<div class='card'><h2>人工反馈</h2>")
    if fb.get("total"):
        add(f"<div class='kv'><div><div class='big'>{fb.get('up', 0)}</div>"
            f"<div class='muted'>判对</div></div>"
            f"<div><div class='big bad'>{fb.get('down', 0)}</div>"
            f"<div class='muted'>判错</div></div>"
            f"<div><div class='big'>{_fmt_pct(fb.get('accuracy'))}</div>"
            f"<div class='muted'>人工标注准确率</div></div></div>")
        add("<p class='muted' style='margin-top:12px'>⚠️ 这个数字天生有偏："
            "你更可能去反馈判错的那几条。只用来看趋势、找反例。</p>")
    else:
        add("<p class='muted'>还没有反馈。回复告警邮件、正文写「对」或「错」即可。</p>")
    add("</div>")

    # ---------------------------------------------------------- 评测分数
    ev = blocks.get("eval")
    add("<div class='card'><h2>判断层评测</h2>")
    if ev and ev.get("arms"):
        add(f"<div class='muted mono' style='margin-bottom:8px'>{esc(str(ev['file']))}</div>")
        add("<table><tr><th>臂</th><th>精确率</th><th>召回率</th><th>根因命中</th>"
            "<th>输入 tokens</th></tr>")
        for arm in ev["arms"]:
            add(f"<tr><td>{esc(str(arm['label']))}</td>"
                f"<td>{_fmt_pct(arm.get('precision'))}</td>"
                f"<td>{_fmt_pct(arm.get('recall'))}</td>"
                f"<td>{_fmt_pct(arm.get('root_accuracy'))}</td>"
                f"<td class='mono'>{arm.get('tokens_in')}</td></tr>")
        add("</table>")
    else:
        add("<p class='muted'>还没有评测结果。"
            "<span class='mono'>python tools/eval_suite.py --rule-only</span> 是最快的起点。</p>")
    add("</div>")

    # ---------------------------------------------------------- 最近运行
    add("<div class='card full'><h2>最近运行</h2><table>")
    add("<tr><th>开始</th><th>任务</th><th>状态</th><th>ok/failed</th><th>AI</th>"
        "<th>摘要</th></tr>")
    for row in (blocks.get("runs") or [])[:15]:
        cls = {"ok": "ok", "degraded": "warn", "failed": "bad"}.get(str(row.get("status")), "")
        add(f"<tr><td class='mono'>{esc(str(row.get('started_at')))}</td>"
            f"<td class='mono'>{esc(str(row.get('task')))}</td>"
            f"<td class='{cls}'>{esc(str(row.get('status')))}</td>"
            f"<td>{row.get('items_ok')}/{row.get('items_failed')}</td>"
            f"<td class='muted'>{esc(str(row.get('ai_status') or ''))}</td>"
            f"<td class='muted'>{esc(str(row.get('summary') or '')[:90])}</td></tr>")
    if not blocks.get("runs"):
        add("<tr><td colspan='6' class='muted'>窗口内没有运行记录</td></tr>")
    add("</table></div>")

    add("</div>")
    add("<footer>只读面板 · 数据来自本机 SQLite 与 data/evals · "
        "刷新页面即可重新查询</footer>")
    add("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------- HTTP

def make_handler(cfg: dict[str, Any]):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "ops-pipeline-dashboard"

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8",
                           render_html(collect_summary(cfg)).encode("utf-8"))
            elif path == "/api/summary":
                body = json.dumps(collect_summary(cfg), ensure_ascii=False, default=str)
                self._send(200, "application/json; charset=utf-8", body.encode("utf-8"))
            elif path == "/healthz":
                self._send(200, "text/plain; charset=utf-8", b"ok")
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # 面板是本机诊断工具：不允许被嵌进别处，也不缓存
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: A003 - 静音，别把访问日志刷进终端
            return

    return Handler


def serve(cfg: dict[str, Any], *, host: str = HOST, port: int = DEFAULT_PORT) -> None:
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    print(f"只读面板: http://{host}:{port}/   （Ctrl+C 停止）")
    print("只绑本机回环地址，不会出现在局域网上。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    # 必须在任何打印之前：面板的 HTML 里有 ⚠️ 之类的字符，
    # 中文 Windows 控制台是 GBK，打印时会抛 UnicodeEncodeError ——
    # 那会把"要显示的状态"变成"看起来像崩溃"。
    from ops.core import make_console_safe

    make_console_safe()

    parser = argparse.ArgumentParser(description="ops-pipeline 只读状态面板")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    parser.add_argument("--host", default=HOST, help="默认只绑 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hours", type=int, default=24, help="统计窗口（小时）")
    parser.add_argument("--json", action="store_true", help="打印汇总 JSON 后退出")
    parser.add_argument("--once", action="store_true", help="渲染一次 HTML 到 stdout 后退出")
    args = parser.parse_args(argv)

    cfg = cfgmod.load_config(args.config)
    if args.json:
        print(json.dumps(collect_summary(cfg, hours=args.hours), ensure_ascii=False,
                         indent=2, default=str))
        return 0
    if args.once:
        print(render_html(collect_summary(cfg, hours=args.hours)))
        return 0
    serve(cfg, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
