# ops-pipeline 设计方案 v0.1

> 个人质量保障与运维流水线 · 基于 DeepSeek Harness 的定时任务编排
> 状态：**已评审，v0.1 已实现（第 1 周交付）**。
> 实现过程中对本文档做了若干调整，全部记录在文末的
> 「附录 A：v0.1 实现与设计的偏差」—— **那部分以附录为准**。

---

## 0. 需要你拍板的决策点

| # | 决策 | 我的建议 | 备选 | 影响 |
|---|---|---|---|---|
| D1 | 调度方式 | **Windows 任务计划程序** 调 `dsh --profile ops` | Python 常驻 APScheduler | 前者崩溃可重入、可手动补跑、无需守护进程 |
| D2 | 巡检会话 | 日报用**固定 session-id**（有记忆）；回归用**新会话** | 全部无状态 | 固定会话能让 AI 说出"昨天同一时间也挂过" |
| D3 | 告警主通道 | **企业微信群机器人** | 钉钉/飞书/Bark/ntfy/Server酱 | 免费、不限量、手机有推送；Server酱免费额度太小，不能当主通道 |
| D4 | AI 调用策略 | **有异常时必调 + 每天一次日报必调**，正常路径不调 | 每次运行都调 | 前者省 token（"无事发生"不烧钱），又不丢日报 |
| D5 | 第一版要不要 Web 展示页 | **不要**，先出 Markdown 报告 | 加个静态页 | 报告先跑通，展示层后置 |
| D6 | AI 模型 | 巡检用便宜模型，归因/日报用强模型 | 统一一个 | 见 §11，靠 profile 的 patch 层区分 |

**另外两个待你确认的前提**（§12 有详述）：
- 你机器能不能长期开机、任务计划程序能不能在无人登录时跑（这决定 D1 是否成立）
- 你倾向哪个告警通道（D3），我需要知道才能给接入代码

---

## 1. 目标与非目标

### 目标
1. 用 **DSH 编排**三条定时流水线，AI 负责"判断、归因、写报告"这三个 cron 做不到的环节。
2. 每个任务都是**可演示的闭环**：采集 → 判定 → 归因 → 报告 → 告警。
3. 产出可公开的作品集：仓库 + README + 架构说明 + 失败复盘。

### 非目标（明确不做，避免范围失控）
- ❌ 不做网页爬虫（合规与稳定性问题，见另一份分析）
- ❌ 不做分布式（个人机器无意义）
- ❌ 不做"AI 自动修复线上问题"（第一版只做发现与报告）
- ❌ 不做多用户/SaaS

---

## 2. 关键前提（已在本机实测，非推测）

以下是设计的地基，全部实际跑过：

| 事实 | 来源 | 对设计的意义 |
|---|---|---|
| `dsh --profile headless "<task>"` = **执行一个任务、答案写 stdout、诊断写 stderr、然后退出** | `dsh headless --help` 实测 | 这就是定时流水线需要的触发原语 |
| `--json` 输出 **NDJSON 事件流**（而非最终消息） | 同上 | Python 侧可解析出结构化事件、token 消耗、工具调用轨迹 |
| `--session-id <id>` 复用**持久化会话**，未知 id 报错 | 同上 | 巡检助手可以有"记忆"，跨运行对照历史 |
| task 可用 `-` 从 **stdin** 读入 | 同上 | 长 prompt 不必塞进命令行（避免转义和长度限制） |
| profile = bundle 组合 + **声明式补丁层**；`cordis.patch.yml` 按 `id` 覆盖配置（如 `agent-default-model` 可指定 provider/model/reasoningEffort） | 读 `~/.dsh/profiles/desktop/` 实测 | 可给 ops profile **单独指定更便宜的模型**，不影响你日常用的 desktop |
| `dsh plugin --profile <name> add <pkg>` 可往 profile 装插件 | `dsh --help` | 后续扩展路径 |
| 本机环境：Python 3.12.10、git 2.55.0、node 已装 | 实测 | 无需新增依赖环境 |
| ⚠️ 无人值守时**权限策略**必须预先放开：当前文件策略为 workspace-write，批准策略为 ask，**没有应答者时请求会 fail-closed** | 本会话运行时上下文 | 见 §12 风险 R1，这是最大的落地障碍 |

> 实测记录：`dsh headless --help` 首次运行会在 `C:\Users\36494\.dsh\profiles\headless` 初始化 profile 目录（DSH 自身的 profile 机制）。

---

## 3. 架构总览

```
┌─ 触发层 ────────────────────────────────────────────────┐
│  Windows 任务计划程序                                    │
│   · 每小时 :05  → 4.2 接口巡检                           │
│   · 每天 08:30  → 4.1 回归 + 2.4 日志扫描 + 日报          │
│   · 每天 23:00  → 心跳：今天的 run 齐了吗                 │
└──────────────────────┬──────────────────────────────────┘
                       │ 调用
┌─ 执行层（确定性 Python，无 AI）────────────────────────┐
│  python -m ops --task <name> --once                     │
│   采集 → 校验 → 基线比对 → 规则判定 → 落库 → 写 run.json │
│   ⚠️ 这一层绝不调用 LLM，任何时候都能独立跑通             │
└──────────────────────┬──────────────────────────────────┘
                       │ run.json（文件契约）
┌─ 判断层（DSH headless agent）──────────────────────────┐
│  dsh --profile ops --json --session-id <id> - < prompt  │
│   读 run.json → 归因 → 分级 → 写 verdict.json + report.md│
│   ⚠️ 这一层只能读 JSON、写 JSON/Markdown，不碰网络和DB    │
└──────────────────────┬──────────────────────────────────┘
                       │ verdict.json（文件契约）
┌─ 产出与告警层（确定性 Python）──────────────────────────┐
│  读 verdict.json → 去重/冷却/升级 → 发企业微信 → 落库     │
│   ⚠️ 由脚本决定"发不发"，不由 AI 决定                     │
└─────────────────────────────────────────────────────────┘
```

### 三条铁律（这是整个设计的核心）

1. **确定性的活给脚本，判断性的活给 AI。**
   网络请求、解析、断言、算分位数、写库 → Python。归因、分级、写报告 → AI。
   反过来做（让 AI 每次重新生成爬虫/测试代码）会得到不稳定、难复现、还烧 token 的系统。

2. **AI 只能读写文件契约，不能直接发告警。**
   AI 产出 `verdict.json`，由 Python 决定是否发送。这样告警逻辑可测试、可审计、可限流，
   **AI 的幻觉不会直接炸你的微信**。

3. **执行层永不依赖 AI 就能跑通。**
   AI 挂掉时系统降级为"纯规则巡检"，功能变弱但不会全哑。

---

## 4. 目录结构

```
ops-pipeline\
├─ DESIGN.md                  # 本文档
├─ README.md                  # 作品集门面（为什么这么设计）
├─ config/
│  ├─ config.yaml             # 目标、阈值、告警、路径（唯一需要改的文件）
│  └─ config.example.yaml
├─ ops/                       # 主包
│  ├─ __main__.py             # 入口：python -m ops --task X --once
│  ├─ core/
│  │  ├─ http.py              # 统一请求：超时/重试/限速/UA（礼貌调用）
│  │  ├─ store.py             # SQLite(WAL) 读写 + 迁移
│  │  ├─ judge.py             # 规则判定：阈值、分位数、连续失败、冷却去重
│  │  ├─ notify.py            # 企业微信/邮件，发送失败重试与落库
│  │  ├─ aibridge.py          # 调 dsh headless，解析 --json 事件流，超时降级
│  │  └─ report.py            # run.json/verdict.json → Markdown/HTML 报告
│  ├─ tasks/
│  │  ├─ api_watch.py         # 4.2 接口可用性巡检
│  │  ├─ regression.py        # 4.1 接口回归测试
│  │  ├─ log_scan.py          # 2.4 日志异常分析
│  │  └─ heartbeat.py         # 心跳：检查预期 run 是否缺失
│  └─ prompts/                # 判断层提示词（版本化，与代码同管）
│     ├─ api_watch.md
│     ├─ regression.md
│     └─ log_scan.md
├─ targets/                   # 4.1 的靶子服务（v1 正常 / v2 注入回归）
│  ├─ app_v1.py
│  └─ app_v2.py
├─ data/
│  ├─ ops.db                  # SQLite
│  ├─ runs/                   # 每次运行的 run.json（原始留痕）
│  └─ reports/                # 生成的报告
├─ scripts/
│  ├─ register_tasks.ps1      # 一键注册 Windows 计划任务
│  └─ run_once.cmd            # 计划任务实际调用的包装（处理 PATH/日志）
└─ tests/                     # 自己的测试（这一步别省，面试会看）
```

---

## 5. 数据契约（接口设计，最关键的一节）

AI 与脚本之间**只靠两个 JSON 文件通信**。这是让系统可测试、可复现、可降级的关键。

### 5.1 `run.json` — 执行层产出（事实，不含判断）

```json
{
  "schema": "ops/run@1",
  "run_id": "20261004T120500Z-api_watch",
  "task": "api_watch",
  "started_at": "2026-10-04T12:05:00Z",
  "finished_at": "2026-10-04T12:05:07Z",
  "status": "degraded",
  "metrics": {
    "targets": 6, "ok": 5, "failed": 1,
    "p50_ms": 412, "p95_ms": 1288, "max_ms": 4310
  },
  "samples": [
    { "target": "frankfurter", "ts": "2026-10-04T12:05:01Z",
      "status_code": 200, "latency_ms": 412, "ok": true },
    { "target": "weather", "ts": "2026-10-04T12:05:02Z",
      "status_code": 502, "latency_ms": 4310, "ok": false,
      "error": "upstream 502" }
  ],
  "baseline": {
    "window_days": 30, "p50_ms": 400, "p95_ms": 1200,
    "latency_percentile": 88.0,
    "failures_last_24h": { "weather": 4 }
  },
  "anomalies": [
    { "kind": "consecutive_failure", "target": "weather",
      "count": 3, "since": "2026-10-04T10:05:00Z" }
  ],
  "artifacts": ["data/reports/2026-10-04/api_watch.html"]
}
```

**注意 `anomalies` 的定位**：它是**规则层给出的线索**，不是结论。判断（严不严重、要不要报警、根因是什么）留给判断层。这个边界要严格守住。

### 5.2 `verdict.json` — 判断层产出（结论，不含副作用）

```json
{
  "schema": "ops/verdict@1",
  "run_id": "20261004T120500Z-api_watch",
  "severity": "notice",
  "should_alert": true,
  "headline": "天气接口连续 3 次 502，疑上游故障；其余 5 个目标正常",
  "root_cause_hypothesis": "上游 502 且延迟同步抬升到 4.3s，倾向对方服务故障而非本地网络",
  "evidence": [
    "12:05 weather 502，延迟 4310ms（基线 p95 1200ms）",
    "过去 24h weather 失败 4 次，其中 3 次连续",
    "同轮其他目标全部 200，排除本地出网问题"
  ],
  "confidence": 0.7,
  "cooldown_minutes": 240,
  "report_md": "data/reports/2026-10-04/api_watch.md"
}
```

约定：
- `should_alert=false` 时**即使 severity 很高也不发**（用于已知噪声/维护窗口）
- `confidence < 0.5` 时脚本自动降级为"仅记录，不告警"
- `cooldown_minutes` 由 AI 建议、脚本裁决（脚本取 `max(AI建议, 配置下限)`）

---

## 6. 三条流水线的详细设计

### 6.1 【4.2】接口可用性巡检 — 每小时

| 项 | 内容 |
|---|---|
| 触发 | 每小时 :05 |
| 输入 | `config.yaml` 里的 targets 列表 |
| 确定性部分 | 请求 + 测延迟/状态码/schema 校验 → 与 30 天基线比 → 算分位 → 落库 |
| 判断部分 | 归因（超时/证书/结构变更/上游故障）、分级、决定是否值得打扰你 |
| 产出 | `run.json` + 每小时静默、有异常才发告警 |
| 数据源 | 免 key：`api.frankfurter.app`、`api.github.com/rate_limit`、`worldtimeapi.org`；需 key：和风天气/高德；**外加一个自建故障靶子** |
| 关键设计 | 自建靶子按概率返回 500。**先证明"监控能抓到已知故障"，才敢相信它在生产里不撒谎**。这一步会在 README 里写成案例 |

### 6.2 【4.1】接口回归测试 — 每天 08:30（+ 手动）

| 项 | 内容 |
|---|---|
| 输入 | `targets/app_v1.py`（正常）、`targets/app_v2.py`（注入 3 类回归：字段改名/类型变化/状态码变化） |
| 确定性部分 | pytest 跑用例 → 出 JUnit XML / pytest-html → 解析成败明细 |
| 判断部分 | **失败归因**：12 个用例挂了，是同一个根因还是 12 个问题？结合用例名与响应体聚类 |
| 产出 | 回归晨报（Markdown）+ 失败时告警 |
| 设计边界 | **PR 门禁交给 CI（GitHub Actions/GitLab CI），DSH 只做"每日全量回归 + 归因 + 晨报"**。事件驱动与代码版本强绑定的活不该由定时器做，这条边界要写进 README |

### 6.3 【2.4】日志异常分析 — 每天 08:30（跑完后接日报）

| 项 | 内容 |
|---|---|
| 数据源 | [Loghub](https://github.com/logpai/loghub)（HDFS/Zookeeper/Apache/OpenSSH…），从小数据集起步 |
| 确定性部分 | 日志解析（Drain 模板提取）→ 按小时窗口计数 → 新旧模板比对 → 与历史基线比 |
| 判断部分 | 哪个错误簇是新出现的、哪个量级暴涨、哪些是已知无害噪声、**根因假设** |
| 回放设计 | 按时间戳**加速回放**（如 100×），一天就能验证一个月的窗口逻辑 |
| 优势 | 部分数据集带人工标注 → 可以算"我检出的异常对不对"，公司日志给不了这个 ground truth |
| 注意 | 第一版只做"发现并报告"，**不做自动修复** |

---

## 7. 存储设计（SQLite，开 WAL）

```sql
PRAGMA journal_mode = WAL;

-- 每次执行（心跳与可观测性的核心表）
CREATE TABLE run (
  id            TEXT PRIMARY KEY,      -- run_id
  task          TEXT NOT NULL,
  started_at    TEXT NOT NULL,         -- UTC ISO8601
  finished_at   TEXT,
  status        TEXT NOT NULL,         -- ok | degraded | failed
  items_ok      INTEGER DEFAULT 0,
  items_failed  INTEGER DEFAULT 0,
  ai_called     INTEGER DEFAULT 0,
  ai_status     TEXT,                  -- ok | timeout | error | skipped
  tokens_in     INTEGER, tokens_out INTEGER,
  exit_code     INTEGER,
  log_path      TEXT
);
CREATE INDEX idx_run_task_time ON run(task, started_at DESC);

-- 通用样本表（接口延迟、状态码等都进这里）
CREATE TABLE sample (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    TEXT NOT NULL REFERENCES run(id),
  target    TEXT NOT NULL,
  metric    TEXT NOT NULL,             -- latency_ms | status_code | ...
  value_num REAL,
  value_text TEXT,
  ok        INTEGER,
  ts        TEXT NOT NULL
);
CREATE INDEX idx_sample ON sample(target, metric, ts DESC);

-- 日志模板（2.4 用）
CREATE TABLE log_template (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source     TEXT NOT NULL,
  template   TEXT NOT NULL,
  first_seen TEXT, last_seen TEXT,
  total_count INTEGER DEFAULT 0,
  UNIQUE(source, template)
);

-- AI 判定留痕（审计：为什么当时报了/没报）
CREATE TABLE verdict (
  run_id        TEXT PRIMARY KEY,
  severity      TEXT, should_alert INTEGER,
  headline      TEXT, root_cause TEXT,
  evidence_json TEXT, confidence REAL,
  model         TEXT, created_at TEXT
);

-- 告警发送记录（幂等的关键）
CREATE TABLE alert (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     TEXT, task TEXT, target TEXT,
  severity   TEXT, channel TEXT,
  sent_at    TEXT, dedup_key TEXT NOT NULL,
  send_status TEXT,                    -- sent | failed | suppressed
  suppressed_reason TEXT
);
CREATE UNIQUE INDEX idx_alert_dedup ON alert(dedup_key);
```

约定：时间统一 **UTC** 存储、展示时转本地；延迟用 REAL 即可（无金额精度问题）；每次运行同时在 `data/runs/` 留一份原始 `run.json`，DB 坏了也不丢证据。

---

## 8. 告警设计

```
判定(规则层) → verdict.json(AI) → 脚本裁决 → 去重/冷却 → 发送 → 落库
                                     │
                                     ├─ 维护窗口 / 已知噪声 → 静默但记录
                                     ├─ confidence < 0.5 → 只记录
                                     └─ 发送失败 → 落库并在下次运行升级重试
```

- **主通道**：企业微信群机器人（免费、不限量、手机推送）
- **备通道**：邮件（SMTP 授权码）
- **去重键**：`{task}:{target}:{severity}`，默认冷却 60 分钟，AI 可建议延长但受配置下限约束
- **恢复通知**：故障消失时发一条"已恢复"，避免"只知道挂了、不知道好了"——这条很多人会漏
- **告警里必须带证据**：当前值 / 基线 / 历史对照 / 直达链接。没有证据的告警等于噪声
- **禁止**：同一 run 重复发送、把 AI 未分析的原始数据当告警发出去（降级时必须显式标注"未经 AI 分析"）

---

## 9. 配置设计（唯一需要你改的文件）

```yaml
# config/config.yaml（实现时改为 config/config.toml，见附录 A1）
project:
  root: ""            # 留空 = 自动使用项目目录，这样整个文件夹可以随便搬
  timezone_display: Asia/Shanghai

ai:
  enabled: true
  profile: ops                      # dsh --profile ops
  cmd: "D:/AITool/deepseek harness/resources/runtime/cli/bin/dsh.cmd"
  dsh_home: data/dsh-home            # 判断层子进程的 DSH home，放进项目内
  model_light: deepseek-flash        # 巡检
  model_heavy: deepseek-flash        # 归因/日报（后续按 profile patch 区分）
  timeout_seconds: 180
  max_calls_per_day: 30
  on_failure: degrade                # degrade | skip

api_watch:
  interval_minutes: 60
  timeout_ms: 8000
  retry: 2
  rate_limit_ms: 1200                # 目标之间最小间隔，礼貌调用
  concurrency: 2
  targets:
    - { name: frankfurter, url: "https://api.frankfurter.app/latest?from=USD&to=CNY", expect: 200 }
    - { name: github_rate, url: "https://api.github.com/rate_limit",        expect: 200 }
    - { name: worldtime,   url: "https://worldtimeapi.org/api/timezone/Asia/Shanghai", expect: 200 }
    - { name: fault_drill, url: "http://127.0.0.1:8788/health", expect: 200, note: "自建故障靶子" }
  thresholds:
    consecutive_failures: 3          # 连续 3 次才告警，抗抖动
    latency_p95_multiple: 3.0        # 超基线 p95 三倍算异常
    cold_start_days: 30              # 冷启动期用绝对阈值

alert:
  primary: wecom
  wecom:
    webhook_env: OPS_WECOM_WEBHOOK  # 只从环境变量读，绝不写进仓库
  email:
    enabled: false
    smtp_host: smtp.qq.com
    to_env: OPS_MAIL_TO
  default_cooldown_minutes: 60
  quiet_hours: "23:30-07:30"         # 除 critical 外静默

schedule:
  heartbeat_expect:                  # 心跳检查"预期该有哪些 run"
    api_watch: 60                    # 每 60 分钟应有 1 次
    regression: 1440
    log_scan: 1440
```

**凭据全部走环境变量**（`.env` + `.gitignore`），仓库里只留 `config.example.yaml`。这条要写进 README，面试官会看。

---

## 10. 失败与降级设计（本方案最值钱的一节）

监控系统最大的敌人不是采不到数据，而是**静默失败**——程序正常退出、你以为"今天没异常"、实际一周没采到数据。

| 故障 | 表面现象 | 防护措施 |
|---|---|---|
| 脚本崩溃 | 无 run 记录 | **心跳任务**独立检查"预期 run 是否缺失"→ 缺失即告警 |
| 脚本正常但采到 0 条 | "一切正常" | **0 条 = 失败**，不是"没事"。显式判空 |
| AI 超时/报错/没额度 | 无 verdict | **降级**：规则层自行判定并发送，标注"未经 AI 分析" |
| AI 幻觉乱报 | 告警轰炸 | AI 只产 verdict；脚本裁决；`confidence` 阈值；上线前 dry-run 一周 |
| AI 漏报（该报没报） | 无声 | 规则层设**兜底硬阈值**，不依赖 AI 也能触发 critical |
| 告警通道挂 | 无人知道 | 多通道 + 发送失败落库 + 下次运行升级重试 |
| DB 写失败 | 数据丢失 | WAL + 重试 + **原始 run.json 落盘兜底** |
| 计划任务没被触发 | 全静默 | 心跳用**不同机制**实现（避免同一故障点） |
| 时区/夏令时错位 | 窗口算错 | 全链路 UTC 存储，仅展示层转换 |

**对应设计**：`on_failure: degrade` —— 任何一环挂掉，系统能力下降但不失声。这一节请重点评审，它决定了这套东西是"玩具"还是"能信的工具"。

---

## 11. 可观测性与成本控制

- **每次运行必留痕**：`run` 表 + `data/runs/*.json` + 日志文件
- **token 成本**：`run` 表记录 `tokens_in/out`，日报汇总"今天花了多少"
- **省钱原则**：**"无事发生"不该调用 LLM**。正常路径不调 AI，只有异常和每日日报才调
- **每日预算上限**：`max_calls_per_day`，超了转降级模式
- **模型分级**：巡检用轻模型，归因/日报用强模型（靠 ops profile 的 patch 层指定，不影响你的 desktop profile）

---

## 12. 实施阶段

| 周 | 交付物 | 验收标准 |
|---|---|---|
| **第 1 周** | `core/` 六个模块 + 4.2 跑通 + 企业微信告警 + 计划任务注册 | **收到第一条由真实故障触发的告警**（用自建靶子造故障） |
| **第 2 周** | 4.1 靶子服务 + 回归用例 + AI 失败归因 + 回归晨报 | 注入的 3 类回归全部被抓到并正确归因 |
| **第 3 周** | 2.4 日志解析 + 模板聚类 + Loghub 回放 + AI 归因 | 在带标注的数据集上给出可解释的异常列表 |
| **第 4 周** | 心跳任务 + 报告统一入口 + README/架构图/失败复盘 | 拔掉 AI 后系统仍能降级运行；README 能直接作为作品集 |

第 4 周的 README 别省——你练这些的最终目的是让人看到你会什么。

---

## 13. 风险与开放问题

| # | 风险 | 说明 | 应对 |
|---|---|---|---|
| **R1** | **无人值守的权限策略** | 当前 DSH 文件策略为 workspace-write、批准策略为 ask，**没有应答者时请求会 fail-closed**。定时任务跑 `dsh headless` 时若触发需批准的操作用户不在场，任务会直接失败 | **已解决（见附录 C）**：子进程 `DSH_HOME` 已重定向到项目内 `data/dsh-home`，profile / 会话 / 存储的写入不再离开工作区，因而不再产生任何需批准的提权；账号凭据单独指回真实 `~/.dsh` **只读**。原本的设计意图保留：判断层只做"读 JSON / 写 JSON"这类无需批准的操作。**已实测验证：受限沙箱内零提权跑通（附录 C）。** |
| R2 | 计划任务在无人登录时能否运行 | Windows 计划任务的"不管用户是否登录"选项与电源策略相关 | 第 1 周实测；不行则退化为"登录后运行 + 错过即补跑" |
| R3 | AI 判断质量不稳定 | 早期 prompt 会产生噪声 | 上线前 dry-run 一周，只记录不发送，人工核对后再开闸 |
| R4 | 公开 API 的礼貌使用 | 高频/并发会被封 | 配置里默认 `concurrency: 2` + `rate_limit_ms: 1200`；只监控有权调用的接口，绝不压测 |
| R5 | 维护成本 | 公开 API 变更会打破解析 | schema 校验失败要归类为"结构变更"而非"服务故障"，两类告警分开 |
| R6 | 成本失控 | 每天多次调用强模型 | `max_calls_per_day` + 成本日报 |

---

## 14. 评审时请重点回答我三个问题

1. **D1/D2/D3 你选哪个？**（调度方式、会话策略、告警通道）——这三个定下来我才能动手。
2. **§10 失败设计里，你觉得哪条最容易被忽略？**——我想确认我们对"静默失败"的重视程度一致。
3. **R1（无人值守权限）你能接受哪种方案？**——是让我把 ops profile 的权限收窄到"只读写项目目录"，还是你更倾向定时任务只做采集、AI 判断改成你手动触发？

确认后我按第 1 周开始实现。

---

## 15. 评审结论（已确认）

| 决策 | 结论 |
|---|---|
| **D1 调度** | Windows 任务计划程序 |
| **D2 会话** | 日报用固定 session-id（有记忆） |
| **D3 告警主通道** | 企业微信群机器人 |
| **R1 权限** | 采用**权限收窄**方案：判断层只读写项目目录内的文件，不碰网络与数据库 |

R1 在架构上这样落地：AI 桥接把 run.json 内容写进提示词（AI 不需要读文件），
并要求 agent 只写 `verdict.json` 与报告两个文件，路径都在项目目录内。
这样无人值守时不存在需要人工批准的操作用户不在场、请求 fail-closed 的问题。

§14 的第 2 问（哪条失败最容易被忽略）未作答，实现时按"静默失败优先"处理：
心跳任务、`0 条 = 失败`、AI 降级标注、发送失败不参与冷却 —— 见 §10 与附录 A。

---

## 附录 A：v0.1 实现与设计的偏差

实现过程中发现设计稿有几处需要调整。**以下以附录为准。**

| # | 设计稿 | 实际实现 | 原因 |
|---|---|---|---|
| A1 | `config/config.yaml`（需 PyYAML） | **`config/config.toml`**（标准库 `tomllib`） | 目标是无人值守运行，**零第三方依赖**：模块全用标准库（urllib/sqlite3/tomllib），换台机器 clone 下来就能跑，不需要 pip 装任何东西。Python 3.11+ 自带 tomllib。 |
| A2 | `alert` 表唯一索引在 `dedup_key` | 唯一索引改为 **`(dedup_key, run_id)`** | 原设计会让同一条告警**永远发不出第二次**（冷却到期也无法重发）。改成"单轮幂等 + 跨轮冷却"，才是正确语义。 |
| A3 | 未明确 | `last_alert_at` 计入 `sent` 与 **`dry-run`**，不计入 `failed` | dry-run 必须**忠实模拟**真实发送，否则演练时看不到冷却效果；而发送失败就该下一轮重试，不能被冷却压住。 |
| A4 | AI 判断失败即视为降级 | 只有"**判断层本来该干活却没干成**"才算降级 | 一切正常时规则层给个 `info`，不是降级。否则健康轮次也被标成 `degraded`，这个信号很快没人看。 |
| A5 | 每小时 → 单独用 `--once` | 同左，另外 **run_id 增加短随机后缀** | 秒级时间戳在同一秒内被触发两次会**撞 ID**，导致样本互相污染、run 记录被覆盖。冒烟测试实际撞到了。 |
| A6 | 报告目录按 UTC 日期 | 报告目录按**本地日期**（由 run_id 的 UTC 时间换算） | 存储一律 UTC，但人是按本地日期翻报告的。换算保持确定性，同一 run_id 永远落在同一目录。 |
| A7 | 未明确 | 目标支持 `enabled = false` | 主配置里的自建故障靶子默认关闭。否则你不启动靶子时它会一直失败，3 轮后变成一条误报 —— 开箱即用的默认值必须是安全的。 |
| A8 | 未配置凭据时发送失败 | 未配置凭据 → **`dry-run`** 并落本地日志，detail 写明原因 | 首次部署的人应该看到"链路通了、只是还没配通道"，而不是"发送失败"。 |
| A9 | 心跳作为普通任务 | 心跳**刻意不使用 AI** | 判据是确定的时间差，交给 AI 只会增加新的失败点。 |
| A10 | 巡检用固定会话 | `session_policy` 按任务配置，**`api_watch` 用新会话** | 每小时一次的独立检查共用一个会话会让上下文无限增长。固定会话留给日报（D2 的本意）。 |

### 已实测验证

* **38 个单元测试全绿**：分位数、模态、异常判定、冷启动、静默时段（含跨午夜）、
  决策抑制、规则兜底结论、存储幂等、冷却、dry-run 语义。
* **端到端冒烟测试**（`config/config.smoke.toml` + 自建靶子）：
  健康轮 → `ok / 无告警`；注入 500 → 前 2 轮仅记录；
  **第 3 轮触发告警**（阈值=3）；**第 4 轮被 `cooldown(0m<60m)` 抑制**。
  run / sample / alert / verdict 四张表逐条核对通过，告警内容已落本地日志并附报告路径。
* **心跳验证**：列出一个"从未运行过"的任务，心跳正确检出并告警、退出码 1。
* **计划任务注册脚本**：`-DryRun` 预览通过；执行策略绕过与 UTF-8 BOM 问题已解决。

### 验证状态（诚实标注；未标注"已解决/已跑通"者为尚未验证）

* **AI 判断层已能在受限沙箱内跑通（附录 C）** —— 早期记录如下，其根因是环境变量继承，
  不是"权限不够"：`dsh --profile headless` 的首次运行会在
  `C:\Users\36494\.dsh\profiles\headless` 初始化 profile，该写入在工作区外，
  被当前沙箱拦截。需要在普通终端里跑一次 `dsh --profile headless "hi"` 让 profile 落地，
  之后才能验证提示词产出与 `verdict.json` 契约。
* **企业微信真实投递**未验证（没有 webhook，全程走 dry-run）。
* **计划任务真实注册**未执行：ScheduledTasks 模块需要 CIM 访问，沙箱内被拒绝。
  需要你在普通 PowerShell（或双击 `scripts\register_tasks.cmd`）里完成。
* 外部公开 API 目标未做长时间稳定性观察。

---

## 附录 B：第 2 周实现记录（AI 判断层 + 回归流水线）

### B1. R1 风险的真实形态（比设计时预判的更具体）

设计时把 R1 写成"无人值守时权限策略可能导致 fail-closed"。实际动手后发现两件事：

1. **`headless` profile 每次启动都会重写 `~/.dsh/profiles/headless/cordis.yml`**，
   这个路径在工作区之外，因此**在受限沙箱里根本无法启动 AI 判断层**。
   这是开发期的限制；**计划任务在沙箱外运行，不受影响**。
2. 真正的落地障碍其实是**凭据**：`headless` profile 默认 provider 是 `deepseek-official`，
   由 `llm-deepseek` 提供，需要 `DEEPSEEK_API_KEY`。而 `desktop` profile 用的是
   `deepseek-account`（账号登录）。**两个插件在 headless 的 bundle 里同样存在**，
   所以用一行 patch 就能切过去：

   ```yaml
   # config/dsh-headless.patch.yml
   - id: agent-default-model
     name: "@deepseek-ai/dsh-agent-default-model"
     config:
       provider: deepseek-account
       model: deepseek-flash
   ```

   用 `dsh --profile headless --patch <本项目内的文件>` 传入，
   **不改动你的 `~/.dsh` 配置**，patch 随项目版本化。

**结论：AI 判断层不需要额外申请 API Key，复用桌面端登录即可。**

### B2. AI 判断层抓到了我自己写的 bug

第一次真实运行 AI 判断层，它的证据里包含一条我没想到的自查：

> `baseline.samples=1` 却记 `consecutive_failures=5`，而 `failures_24h=1`
> —— 24 小时内仅 1 次失败却存在连续 5 次失败，时间跨度无法判断。

它是对的：`consecutive_failures` 当初没有时间窗口，昨天 17:05 的失败被算进了今天的
"连续失败"，与有窗口的 `failures_24h` 自相矛盾。

修复（B3）后它自己确认：`两个窗口在本轮互相自洽`。

**这条 bug 不是测试发现的，是判断层发现的** —— 一个只会打印指标的系统永远不会告诉你
指标之间互相矛盾。这是"AI 负责判断"最有力的实证。

### B3. 相对第 1 周的代码变更

| # | 变更 | 原因 |
|---|---|---|
| B3-1 | 新增 `core/pipeline.py`：判定→报告→裁决→告警→落库 | **收尾逻辑必须只有一份**。复制到第二条流水线意味着冷却、降级、幂等键会各自演化，最后出现"这条被抑制、那条没有"的不一致 —— 极难发现的 bug |
| B3-2 | `store.failure_streak()`：连续失败**限定 24 小时窗口**，并输出 `streak_span_minutes` | B2 的 bug。只说"连续 5 次"不够：5 次可能跨 5 分钟也可能跨 5 天 |
| B3-3 | 新增 `core/checks.py`：接口契约断言 DSL | 用例变成纯数据；失败明细天然结构化（期望 vs 实际），AI 归因不必解析文本报告；DSL 自己有独立单测（它错了整套回归就是假绿） |
| B3-4 | 新增 `targets/service.py`（v1/v2 变体）+ `targets/cases.py`（7 条用例） | 你无法在真实项目里故意弄坏接口；v2 注入三类最常见的回归：字段改名、类型变化、状态码语义变化 |
| B3-5 | 新增 `tasks/regression.py` | 自带启停靶子服务，计划任务不依赖"你得先手动启动某个服务" |
| B3-6 | `aibridge.diagnose()`：把 dsh 原始报错翻译成"人话 + 怎么办" | 静默失败的反面不是"报错"，而是"报错但看不出该怎么办"。且失败原因要打到 stdout（运维第一眼看 launcher.log），不能只躺在报告附注里 |
| B3-7 | 新增 `--override KEY=VALUE` | 运维上"临时改个参数重跑一次"很常见（例如换 variant 复现问题），不该为了跑一次去改配置文件 |
| B3-8 | 报告/告警渲染支持 `item_label` | 接口巡检说"目标"，回归说"用例"，同一套渲染器不写死措辞 |
| B3-9 | 测试时间戳全部改成"相对现在" | 硬编码时间戳的测试会随真实时间流逝悄悄失效 —— 那比没有测试更坏 |

### B4. 第 2 周验证结果

* **66 个单元测试**全绿。
* **AI 判断层真实跑通**：`ai=ok`，`verdict.json` 通过契约校验，报告文件按契约写出。
* **降级路径**：`ai.cmd` 指向不存在的路径 → 明确诊断 + 规则层兜底 + **告警照发**并标注"未经 AI 分析"。
* **回归流水线**：v1 全绿 7/7；v2 抓到全部 3 个注入回归。
* **AI 归因质量**：正确判定"3 个独立回归、非单点故障"，并给出未写在提示词里的交叉推理
  （"`/api/users/9999` 返回了正确 404，说明不是全局约定变更，而是该接口自身缺失未找到分支"）。

### B5. 更新后的未验证清单

* 企业微信**真实投递**（无 webhook，全程 dry-run）。
* **计划任务真实注册**（需在普通 PowerShell 中执行）。
* 外部公开 API 目标的长时间稳定性。
* **日志异常分析流水线**（原计划第 3 周内容，尚未实现）。

---

## 附录 C：第 3 次修复记录 —— 判断层的沙箱隔离

### C1. 现象

在受限沙箱（DSH 文件策略 `workspace-write`、批准策略 `ask`）里跑
`python -m ops --task log_scan --once`，每次都会触发**提权申请**；而弹窗只有
「拒绝 / 允许一次」、**没有"始终允许"** —— 也就是每调用一次判断层就要人工点一次，
无人值守时更是直接 fail-closed。

### C2. 根因：不是"需要权限"，而是环境变量串了

`dsh --profile headless` **每次启动都会重写** `<DSH_HOME>/profiles/headless/cordis.yml`。
而 `aibridge._run_once()` 的 `subprocess.run(...)` 当时没有传 `env=`，子进程于是完整继承了
编排层（桌面 DSH 会话）的 `DSH_*`：

| 继承来的变量 | 实测值 | 后果 |
|---|---|---|
| `DSH_HOME` | `C:\Users\36494\.dsh` | profile 被写到工作区之外，必然被沙箱拦 |
| `DSH_PROFILE_DIR` | `...\profiles\desktop` | 已经传了 `--profile headless`，目录变量却指着 desktop |
| `DSH_SESSION_ID` / `DSH_SHELL` / `DSH_WEB_URL` | 编排层自己的会话 | 把外层会话语义带进判断层 |

这些变量本身是 DSH **注入给模型 shell 工具的"事实"**，不是 boot 的输入
（`@deepseek-ai/dsh-shell-env`：ambient `DSH_*` 会被 executor 丢弃后按注册表重新注入），
所以整体剔除是安全的，不会丢掉判断层需要的东西。

### C3. 修复

| # | 变更 | 依据 |
|---|---|---|
| C3-1 | `AIBridge._child_env()`：剔除全部继承的 `DSH_*`，只设 `DSH_HOME` | 见 C2 |
| C3-2 | 新增 `ai.dsh_home`（默认 `data/dsh-home`，相对路径按项目根解析） | 把 profile / 会话 / 存储 / 缓存的写入全部内化进工作区 |
| C3-3 | `config/dsh-headless.patch.yml` 新增 `credentials` 条目，`dshHome: "~/.dsh"` | 账号令牌住在 `$DSH_HOME/.credentials.yaml`，跟着一起搬会报 `ACCOUNT_SIGN_IN_REQUIRED`；`dshHome` 经 `resolveDshHome → expandHomePath` 支持 `~` 前缀，所以不写死任何机器路径 |
| C3-4 | `ai.log` 头部记 `dsh_home=...` | 事后复盘要能一眼看出那次跑用的是哪个 home |

### C4. 验证

* `dsh --dump-config`（**零额度**）确认两件事：profile 落在
  `data/dsh-home/profiles/headless/`；组合后的配置树里 `- id: credentials` 带上了
  `dshHome: ~/.dsh`。
* 一次真实判断层调用：`returncode=0`、4.8s、`verdict.json` 通过契约校验、
  **零提权请求**；期间没有出现 `ACCOUNT_SIGN_IN_REQUIRED`，证明凭据读取路径正确。
* `tests/test_aibridge_env.py`：7 条断言锁住"子进程环境里只保留我们指定的 `DSH_HOME`"。
* 全量单元测试 140 条全绿。

### C5. 已知代价（诚实标注）

* **令牌刷新会失败**：401 后 `rejectToken` 需要写 `~/.dsh/.credentials.yaml`，而它在工作区之外，
  受限沙箱里这次写入会被拒。该写入在调用侧被 `try/catch` 吞掉，**不影响本次判断结果**，
  但判断层无法自行更新或清除过期令牌 —— 那件事仍由桌面端负责。
* **不再继承 `~/.dsh/AGENTS.md`**：判断层的 home 换了，用户级全局指令不再生效。
  对无人值守的判断层而言这是好事（不受桌面端个人偏好干扰），但需要知道这个变化。
* `.anonymous-user-id` 会与新 home 绑定，与桌面端不再是同一个值。

---

## 附录 D：第 3 周实现记录 —— 日志异常分析流水线

对应设计稿 §6.3。目标：把"一行行日志"变成"可统计、可比较、可归因的错误簇"。

### D1. 为什么自己实现 Drain，而不是装个库

零第三方依赖是硬约束（无人值守下依赖越少越不容易半夜挂掉），而 Drain 本身不长：
取级别与时间戳 → token 归一（数字/IP/UUID → `<*>`）→ 前 depth 个 token 分桶 →
桶内按"相同位置占比"算相似度 → 达标则并入并把差异位置泛化，否则新建模板。

**已知取舍（写进了代码注释和 README）**：模板提取的目的是聚类，不是逐字段区分。
`status=200` 与 `status=500` 会被归进同一模板（数字被泛化），
所以严重性靠**日志级别**判断，不靠模板里的状态码。这是刻意的。

唯一一处"加料"：**把级别放进 token 序列的首位**。否则 `INFO ...` 与 `ERROR ...`
可能因正文相似被并成同一模板，错误就被埋进正常流量里了。

### D2. 窗口设计：第一轮就有基线

设计稿原本写的是"拉取过去 30 天价格曲线"，那会有冷启动空转的问题（见附录 A）。
日志这里换了个做法：**同一个文件里比两个紧邻窗口**（最近 W 小时 vs 之前 W 小时）。

| 好处 | 说明 |
|---|---|
| 无冷启动 | 只要文件里有 2×W 小时的数据，第一轮就能比较，不必等 30 天 |
| 参照物稳定 | 前一窗口是"固定不变的过去"，不会随历次检测漂移 |
| 历史库作补充 | DB 里累积的正常窗口作为中位数参照，仅在前一窗口缺该模板时使用 |

### D3. 三个 bug —— 其中两个是 AI 判断层抓到的

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| D3-1 | 模板长成 `ERROR ERROR [db] ...` | 只剥了时间戳、没剥级别词，而级别又作为首 token 前置了一次 | `_find_level()` 定位级别并**从正文摘掉**，规范级别再前置 |
| D3-2 | 第 2 轮起故障"消失"，`status=ok` | 故障期的量级被写进历史基线，参照值被抬高到与当前值相同 | 基线**只由正常窗口构成**：`log_window.anomalous` 标记故障期窗口并排除；参照值优先取同文件前一窗口，退路是历史**中位数**（不用均值、不取两者最大值） |
| D3-3 | 首轮告警若被静默/冷却抑制，就**永远不再报** | "新模板"判据是"历史窗口里没有记录"，第二轮它就有记录了 | 判据换成 **`first_seen` 是否在宽限期内**（默认 3 天）；持续故障由冷却机制管住噪声，而不是被误判成"新常态" |

**D3-2 与 D3-3 的后果都是静默失败** —— 正是这套系统存在的意义所要防的那类问题。
AI 在证据里写下的原话是：

> "39.3 倍'突增'更可能是上一窗口 220 次异常偏低造成的对比假象"
> "`history_avg`/`history_median` 均为 379 且 `history_windows=5`，与'历史从未出现'矛盾"

它没有权限改代码，但它把矛盾指出来了 —— 而矛盾正是 bug 的外显。

修复前后的实测对比：

| 轮次 | 修复前 | 修复后 |
|---|---|---|
| 第 1 轮 | 检出 → 告警 | 检出 → 告警发出 |
| 第 2、3 轮 | `status=ok` **静默** | `severity=alert` **仍检出**，由冷却抑制重复 |

### D4. 代码变更

| # | 变更 | 说明 |
|---|---|---|
| D4-1 | `core/logparse.py` | Drain 模板提取；多格式时间戳（含 Z / +08:00 / 方括号 / 斜杠）、级别别名归一、空行不产生模板 |
| D4-2 | `core/store.py`：`log_template` / `log_window` 两表 + **列迁移机制** | 新增列时老库能平滑升级（`CREATE TABLE IF NOT EXISTS` 不会加列） |
| D4-3 | `core/store.py`：`log_template_history()` | 返回 counts/median/first_seen/excluded_windows；默认排除故障期窗口 |
| D4-4 | `tasks/log_scan.py` | 6 类判据：`new_error_template` / `count_spike` / `log_stale` / `source_missing` / `source_empty` / `parse_ratio_low`；前三类是"没数据也可能是故障"的防护 |
| D4-5 | `tools/loggen.py` | 合成日志生成器，可注入新错误模板与量级暴涨，**并打印注入量作为 ground truth** |
| D4-6 | `report.py` 增加 `item_label` / `value_label` / `value_suffix`；延迟行按数据存在与否决定打不打 | 日志分析没有"延迟"，硬套会显示成 `8682ms`（其实是次数），那是误导 |
| D4-7 | `config.set_path()` 支持**列表下标** | `--override log_scan.sources.0.path=...` 是换日志文件重跑的唯一手段；原先只走字典键，会直接把数组覆盖成字典 |
| D4-8 | `config._check_numeric_on_dict()` | 字典层出现纯数字键时明确报错。否则 `--override api_watch.0.url=...`（漏了 `targets`）会静默创建一个没人读的键 |
| D4-9 | `aibridge.diagnose()` 增加 profile 目录不可写的指引 | 与附录 C 的修复配套：现在这条错误本身就指向 `ai.dsh_home` |

### D5. 验证结果

* **140 个单元测试**全绿（含 `tests/test_logparse.py`、`tests/test_log_scan.py`、`tests/test_config.py`）。
* **干净日志**（18757 行，无注入）：8 个模板，`status=ok`，不告警。
* **注入日志**（+379 行新错误模板、+8517 行量级暴涨）：
  * `new_error_template`：379 次 —— **与注入量完全一致**；
  * `count_spike`：8679 次 vs 参照值 193 次 = **45.0 倍**；
  * `severity=alert`，AI 归因 `ai=ok`。
* **持续故障不退化**：连续三轮都检出，第 2、3 轮由冷却抑制而非静默。
* **基线卫生**：18 条窗口记录中 4 条被标记为故障期并排除在基线之外。
* **AI 归因质量**：它主动写出了 `excluded_windows=2（历史故障窗口未计入基线）`，
  说明提示词里对字段语义的交代是有效的。

### D6. 已知限制

* 尚未在**真实业务日志**上跑过（目前是合成日志 + 注入异常）。换真实日志只需改
  `[[log_scan.sources]]` 的 path，但时间戳格式若不在支持列表内，会落到 `parse_ratio_low`
  这条线索上 —— 那是提示，不是静默。
* 需要 2×`window_hours` 的日志量；轮转过快会让基线窗口为空（会退到历史基线或标记冷启动）。
* 模板折叠变量（见 D1），状态码级别的区分做不到。

### D7. 上线记录（本轮收尾）

* **副本收敛为一份**：正本 `D:\AITool\ops-pipeline`；桌面改为指向它的**快捷方式**
  （`ops-pipeline.lnk`），不再保留第二份代码。删除前逐文件哈希核对 173 个文件、
  0 处不一致；桌面副本独有的 2 个文件是早先验证"副本能独立运行"时产生的测试痕迹。
* **计划任务真实注册**：`OpsPipeline-ApiWatch`（每小时）、`OpsPipeline-Regression`（08:30）、
  `OpsPipeline-Heartbeat`（23:00），均 State=Ready。手动 `Start-ScheduledTask` 后
  `LastTaskResult=0`，`data/logs/launcher.log` 里能看到计划任务真实跑出的输出 ——
  「计划任务 → run_once.cmd → python -m ops」整条链已实测。
* **`log_scan` 默认不注册**：它的日志源默认还是演示用的合成日志，
  真注册会因文件停止增长而持续报 `log_stale`（对演示文件是误导性告警）。
  改为显式 `-IncludeLogScan`；`heartbeat_expect` 也同步只声明实际注册的项 ——
  声明了却没注册，心跳会一直报"从未运行过"。这是同一条原则：
  **默认配置必须开箱即用，不该一装上就产生误报。**

---

## 附录 E：告警通道落地（企业微信）

### E1. 一个会导致静默误判的坑：HTTP 200 不等于投递成功

企业微信群机器人接口**即使 key 无效也返回 HTTP 200**，真正的结果在响应体的
`errcode` 里（成功为 0，失败如 `93000 invalid webhook url`）。

原先 `_send_wecom()` 只看 HTTP 状态码，于是"webhook 配错了"会被记成 **sent**。
这是本项目最想消灭的那类失败：**监控照跑、报告照生、你以为没异常，其实消息根本没发出去。**

修法：`http.post_json()` 改为返回解析后的响应体，`_send_wecom()` 显式要求
`errcode == 0`；响应不是 JSON 也判为失败（无法确认投递结果）。

**已用真实网络验证**：写入格式合法但 key 无效的 webhook → 企业微信返回
`errcode=93000` → 程序正确判为 `failed` 并原样附上 errmsg。

### E2. 凭据来源：环境变量优先，本地密钥文件兜底

新增 `config/secrets.local.toml`（已在 `.gitignore` 中），键名与环境变量名一一对应。
读取顺序：**环境变量 → 本地密钥文件**。

为什么要两条路：**计划任务不一定能及时看到新设的环境变量** ——
Task Scheduler 服务的环境块可能被缓存，改完要重新登录才生效，
而无人值守的场景恰恰不能依赖"你重新登录一次"。文件是每次进程启动现读的。

配套 `scripts/set-webhook.cmd`：交互式粘贴 → 校验前缀 → 写入文件 → 立刻发一条测试消息。
`-Show` 打码显示当前来源（含"环境变量会盖住文件"的提醒），`-Remove` 移除。

### E3. 顺手修掉的两个可用性 bug

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| E3-1 | 用记事本改过的配置直接解析失败 | Windows 记事本保存 UTF-8 默认**带 BOM**，而 TOML 解析器遇到 BOM 会报错 | `_read_toml()` 显式剥离 BOM，配置与密钥文件都走它 |
| E3-2 | `tools/notify_test.py` 打印失败提示时**抛 UnicodeEncodeError** | 控制台默认 GBK 代码页，`✗`/`✓` 编码不了，本该打印的提示变成了 traceback | 控制台输出只用 ASCII 标记（`[OK]` / `[!!]`）；emoji 只留在发往企业微信的消息体里（那是 UTF-8 HTTP，不受控制台影响） |

`tests/test_notify.py` 覆盖了上述全部行为（errcode 成功/失败/非 JSON/HTTP 错误、
凭据来源优先级、密钥文件读取与 BOM 容错）。测试总数 151。

### E4. 三个通道，以及为什么最后选了「自建应用」

| 通道 | 前置条件 | 结论 |
|---|---|---|
| `wecom`（群机器人） | **普通内部群** | ❌ 对本机不适用：全员群 / 单人企业里「添加群机器人」的入口根本不出现（社区有多篇同现象提问），且把个人微信拉进群会变成外部群，同样不支持 |
| `wecom_app`（自建应用） | 后台拿 CorpID / Secret / AgentId | ✅ **单人企业也能用**，成为主通道 |
| `email` | SMTP 授权码 | ✅ 保留为 fallback；最通用，但手机上不如聊天消息显眼 |

`wecom_app` 的实现要点：

* **access_token 本地缓存**（`data/state/wecom-token.json`，提前 5 分钟续期）。
  官方有效期 7200 秒且有配额，每小时跑一次的巡检没必要每轮都换新的。
  ⚠️ 该缓存文件**内含密钥**，`data/` 不要分享出去。
* **`invaliduser` 也要判失败**：`errcode=0` 但接收人为空，说明应用的可见范围没包含你 ——
  只看 errcode 会漏掉这种情况，消息实际上没送到任何人手里。
* **`errcode=40014`（token 失效）时清掉缓存**，下一轮自动重取。
* 通道语义统一为：`sent`（接口确认）/ `dry-run`（**没配**，没尝试）/ `failed`（配了但发不出去）。
  三者分清，才能避免"没配"被误报成"发送失败"，也避免"失败"被误报成"没配"。
* 新增 `alert.fallback` 显式声明备用通道（默认 `["email"]`）。
  全部失败时把**每个通道的结果**汇总进 detail，免得只看到最后一条而误判原因。

### E5. 验证方式（不用真实凭据也能验证链路）

真实 webhook 只有你自己能创建，所以链路验证用「格式合法但内容无效」的凭据做：

* 群机器人：假 key → `errcode=93000 invalid webhook url` → 正确判 `failed`
* 自建应用：假 CorpID → `errcode=40013 invalid corpid` → 正确判 `failed`，
  并在提示里写明"常见原因：CorpID/Secret 抄错"

这条路径能证明：**凭据读取 → HTTP 调用 → 响应解析 → 结果判定**整条链是通的，
只差把真值填进去。失败时还验证了不留垃圾（无 token 缓存文件残留）。

### E6. 最终落地：邮件，以及两个把"凭据错误"伪装成"网络问题"的坑

四条通道逐一实测后的结论：

| 通道 | 实测结果 |
|---|---|
| 群机器人 | 需要普通内部群；**全员群 + 单人企业拿不到入口**，且拉个人微信会变成外部群（同样不支持） |
| 自建应用 | 要填「企业可信IP」，而它要求先配**可信域名**（需 ICP 备案）或**接收消息服务器URL**（需公网服务器） |
| ntfy | 服务可达（实测 2.1s），但**国内安卓应用商店不让装**（"仅支持出境用户安装"） |
| pushplus | 实测 `code=905 账户未进行实名认证`，而实名要付费（¥3.9 / ¥10） |
| **email** | ✅ **唯一没有前置门槛的方案**：不要实名、不要付费、不经第三方、无额度上限 |

邮件跑通的过程中踩到两个**同类**的坑，都值得记下来：

**坑 1：错误信息把"授权码错了"说成"网络问题"。**
`smtplib.login()` 会优先选 `AUTH PLAIN`，而 QQ 邮箱的反滥用逻辑（服务端主机名
`newxmesmtplogicsvr…`）对 PLAIN 的处理是**直接掐断连接**。于是真正的
`535 Login fail. Account is abnormal…` 被吞掉，调用方只看到
`SMTPServerDisconnected: Connection unexpectedly closed` —— 排查方向被彻底带偏
（会去查端口封锁、防火墙）。

实测对比（同一个错误授权码）：

| 认证方式 | 结果 |
|---|---|
| `AUTH PLAIN`（smtplib 默认） | `SMTPServerDisconnected` —— 看不出原因 |
| `AUTH LOGIN`（强制） | `535 Login fail. Account is abnormal, service is not open, password is incorrect…` —— 原因一目了然 |

修法：`smtp_login()` 优先用 `AUTH LOGIN`，并把 535 原文带进错误信息。

**坑 2：构造函数的隐式 EHLO 没填能力表。**
`smtplib.SMTP_SSL(...)` 内部会发 EHLO，但实测中 `esmtp_features` 是**空的**，
于是"服务端是否支持 LOGIN"判断错误、又退回 PLAIN —— 绕回坑 1。
修法：发现能力表为空时显式补一次 `ehlo()`。

两个坑的共同点：**都是把"凭据不对"伪装成"网络不通"**。这与本项目反复强调的原则一致 ——
报错必须告诉人该去改什么，否则排查成本全落在使用者头上。

### E7. 验收证据

```
$ python -m ops --task log_scan --once   # 用注入异常的日志触发真实告警
[log_scan] ... status=degraded severity=alert alert=sent

数据库：
  级别: alert   通道: email   投递状态: sent   详情: → 3649496820@qq.com
```

`send_status='sent'` 是**接口确认投递成功**的标志（不是"尝试过"）。
至此"采集 → 规则判定 → AI 归因 → 裁决 → 真实投递 → 落库"整条链闭环。

---

## 附录 F：补齐设计稿剩余条目（恢复通知 / 日报 / token 成本）

设计稿里还有三件事没做完，这一轮补上。

### F1. 关键技术前提：先摸清事件流的真实 schema

动手前先把 `dsh --json` 的事件流 dump 出来看清楚（而不是靠猜）：

```
{"type":"session","sessionId":"session-ff7e036a-…","cwd":"D:\\AITool\\ops-pipeline"}
{"type":"thinking",…,"usage":{"inputTokens":2294,"outputTokens":966,
   "cacheReadTokens":5760,"cacheWriteTokens":0,"totalTokens":9020}}
```

两个要点：

* **`totalTokens` 是会话累计值，`inputTokens`/`outputTokens` 是单次调用的值。**
  所以用量要按单次字段求和 —— 把 `totalTokens` 加起来会把总量重复计算。
  这条写进了代码注释和单测（`test_does_not_sum_cumulative_total`）。
* **`sessionId` 可以结构化解析**，不必用正则从 stdio 里捞。这直接让 D2
  （日报固定会话）从"尽力而为"变成"可靠"。

### F2. §8 恢复通知

设计稿原文："故障消失时发一条'已恢复'，避免'只知道挂了、不知道好了'。"

实现要点：

| 决策 | 选择 | 理由 |
|---|---|---|
| 跟踪哪些告警 | **只跟踪 `send_status='sent'`** | dry-run 意味着当时没配通道、没通知过任何人，给它发"已恢复"没有意义，还会把日报的"未恢复"数字撑成噪声（实测踩到：6 条里 5 条是这种） |
| 恢复判据 | 本轮**没有任何异常线索**且判断层给 `info` | 与告警用同一套判据，避免"告警靠 A 逻辑、恢复靠 B 逻辑"导致状态错位 |
| 静默时段 | 恢复通知**也被静默** | 好消息也不该半夜吵人；保持未恢复状态，出了静默时段再通知 |
| 落痕 | 标记 `alert.resolved_at` + 写进报告附注 | 事后能回答"它到底恢复没有" |

**实测**：注入异常 → 收到告警邮件；换回正常日志 → 收到「已恢复」邮件，
数据库里那条告警的 `resolved_at` 被填上。

顺带修了一个顺序 bug：恢复通知原本写在**报告生成之后**，所以它没进报告附注
（只在邮件和数据库里有痕）。改成"先算报告路径、最后落盘"，报告里才有完整的本轮动作。

### F3. §12 报告统一入口：每日日报

它和另外三条流水线的**语义不同**，所以**没有走 `pipeline.finalize`**：

* `api_watch` / `regression` / `log_scan` 是**异常告警**：没异常就不出声；
* `daily_report` 是**定期汇总**：不管有没有异常都发一份，
  让"今天一切正常"也成为一条明确信息（而不是靠沉默去推断）。

复用了 `aibridge` / `notify` / `report` / `store`。两个设计细节：

* **判断层用固定会话**（D2）—— 所以它能指出跨天趋势；
  这也是 `ai.session_policy` 从"写了没处用"变成真正生效的地方。
* **日报正文优先用 AI 写的那份**（它就是给人读的），AI 挂了才退到确定性渲染。
  确定性版本不是摆设：`report.render_daily_markdown()` 有独立单测，
  保证"AI 全挂"时仍有一份能看的日报。
* **每天最多发一次**：用 `dedup_key = daily_report:-:digest:<本地日期>` 复用现有幂等机制。
* 日报自身的投递记录**直接标记为已恢复**——它不是故障，不该挂在"未恢复告警"里。

**实测**：一份 AI 日报抓到了两个真问题 —— 未恢复告警清单，
以及"token 全为 0，可能是用量采集未接入"（当时确实是窗口内的历史运行都没有该字段）。

### F4. §11 token 成本

`run` 表早就留了 `tokens_in/tokens_out` 字段，但一直是空的。现在从事件流解析并写入，
并按"cache 读的单价远低于普通输入"这一事实**分列存储**：

```
tokens_in=9413  tokens_out=4645  cache_read_tokens=73344  cache_write_tokens=0
```

列是加在已有表上的，走的是第 3 周引入的**列迁移机制**（`COLUMN_MIGRATIONS`）——
老库打开时自动补列，不需要手工 SQL。

**不做的事**：不把 token 直接换算成"钱"。cache 读/写的单价差异大，
没有准确单价时给一个看着精确的数字比不给更糟 —— 日报里只报用量与分列明细。

---

## 附录 G：告警邮件改成"讲人话"

### G1. 问题来自真实反馈

第一封真实告警邮件发出去之后，收件人（非开发者）的原话是：
"看起来有点杂乱无章，且不太懂里面的内容。"

把邮件翻出来看，问题一目了然 —— **我把判断层的 `evidence` 原样贴进了邮件**：

```
- samples: worldtime ok=0, ts=2026-10-05T08:37:53Z, status_code=null,
  error="URLError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。"
- baseline.worldtime: samples=0, consecutive_failures=5, streak_span_minutes=887.9,
  failures_24h=5, p50_ms=0.0, p95_ms=0.0, modal_body_hash=null
- anomalies: kind=consecutive_failure, target=worldtime, ...
- metrics: targets=3、ok=2、failed=1、anomalies=1、status=degraded
```

这些内容**对写代码的人勉强可读，对收邮件的人就是一堵墙**。
更要命的是中间还有一句"同窗口自洽：consecutive_failures=5 与 failures_24h=5 相等
（两字段窗口不同）……"—— 那是我当初为了让模型**自证严谨**而要求的推理过程，
它属于报告，不属于提醒。

### G2. 根因：搞错了"邮件"这种媒介的职责

邮件不是分析报告，它是**拍肩膀**。一个人在地铁上掏出手机看一眼，
只想知道三件事：

1. **出什么事了**
2. **要紧吗**
3. **我该做什么**

技术细节应该留在 `report_path` 指向的报告文件里 —— 想深究的人自然会点开。

### G3. 改法：给判断层加三个"人话字段"

`ops/verdict@1` 契约新增（都写进了 4 个提示词，并附正反例对照表）：

| 字段 | 作用 | 提示词里明确禁止的写法 |
|---|---|---|
| `plain_summary` | 2~3 句大白话：发生了什么 | `samples: worldtime ok=0, status_code=null` |
| `impact` | 一句话：这意味着什么 | `metrics.targets=3, ok=2, anomalies=1` |
| `suggested_action` | 一句话：建议做什么 | `检查 baseline.consecutive_failures 与 failures_24h 的窗口一致性` |

`evidence` 保留原样，但**只进报告文件**。`render_markdown()` 只渲染这三块，
外加一行统计和报告路径；`evidence` 仅在**没有** `plain_summary` 时才兜底展示
（宁可展示技术细节，也不能发一封空邮件）。

### G4. 降级 ≠ 难读

判断层不可用时走规则层兜底 —— 如果不管它，"降级"就意味着收件人收到一封
满屏 `consecutive_failure` 的邮件。所以规则层也做了翻译
（`judge.ANOMALY_PLAIN`）：

```
consecutive_failure → 有个目标连续失败，一直连不上
structure_change    → 有个目标返回的内容和以前不一样了（可能对方改了接口）
log_stale           → 日志停止写入了（服务可能停了）
stale_run           → 有任务没有按预期运行（可能没触发或中途挂了）
```

未收录的类型退化成"发现异常（kind）"，**不猜、不编**。

### G5. 效果对比

同一份判断结果，改之前 30 行机器字段 → 改之后 11 行人话：

```
🔴 严重　日志已停写超过24小时，服务很可能已经停了

这个应用已经一天多没有产生任何新日志，最后一条日志停在约24小时前，本轮窗口里
一条记录都没有。在停写之前的那个窗口，数据库连接失败和缓存因内存不足清理失败的
报错明显增多。现在看不到任何新日志，所以"没有报错"并不等于正常。

**这意味着**：服务很可能已经停止运行，或者日志采集/轮转出了问题，这段时间内的
故障完全不可见，用户请求可能已经在失败。

**建议**：请先确认这个应用的进程是否还在运行、日志文件是否被轮转或采集路径是否
失效，然后再检查它的数据库连接和缓存内存情况。

———
日志源 0/1 正常，失败 1
完整报告：D:\AITool\ops-pipeline\data\reports\...\log_scan-....md
```

**并且加了测试守着它退化**：`test_does_not_leak_machine_fields` 断言邮件正文里
不许出现 `samples:` / `baseline.` / `modal_body_hash` / `p95` / `status_code`。
没有这条测试，下次改渲染逻辑很容易又倒回去。

### G6. 顺带：日报也要能"扫一眼"

日报正文比告警长得多，所以在提示词里要求**开头必须有「30 秒版」**：
整体怎么样（1 句）、有没有要处理的事（1 句）、是什么、怎么处理（1~2 句），
之后才是详细部分。同时要求把术语翻成人话（`degraded` → "跑完了但发现了问题"）。

---

## 附录 H：判断层评测（eval）

### H1. 为什么要做这个

前七个附录解决的都是"怎么做对"。但有一个问题始终没有答案：

> **你怎么知道这套判断是有效的？**

在此之前，所有"AI 抓到了真问题"的说法都来自个案观察 ——
我跑一次、看它答得不错，就认为它行。这是**叙事**，不是**证据**：
它无法回答"改了提示词之后是变好了还是变差了"，
也无法回答"这层 AI 相比纯规则到底多值多少钱"。

所以补上评测脚手架。它是这个项目里唯一能让别人**独立复现**结论的部分。

### H2. 设计取舍

| 决策 | 选择 | 理由 |
|---|---|---|
| 夹具形态 | **冻结的 JSON**（`evals/golden/*.json`），不是运行时现算 | 评测要度量固定靶子。现算的话一改判定逻辑输入就跟着变，分数波动分不清是"模型变好"还是"题目变简单" |
| 夹具来源 | 按 **55 份真实 run.json** 的结构建模，再由 `evals/build.py` 生成 | 手写容易脱离现实；完全复制真实样本又无法覆盖"从未发生过的题型"（尤其是反向用例） |
| 一致性 | `validate_run_doc()` 强制校验 | 自相矛盾的输入会让模型"答错" —— 那是题目有问题，不是模型有问题 |
| 对照臂 | **规则层 + AI 并排跑** | 只报 AI 绝对分说明不了好坏。"准确率 85%" 只有在规则层是 60% 时才有意义 |
| 根因评分 | 关键词命中（确定性） | 便宜、免费、可解释、适合回归检测。语义评分要上 LLM-as-judge，见局限 |
| 精确率 vs 召回率 | 两者都报，但强调**精确率** | 告警系统死于误报。召回率低的后果是漏报，精确率低的后果是**用户关掉告警** |

### H3. 一个意外发现：`run@1` 契约并非各任务统一

写一致性校验器的第一版就直接把 `log_scan` 判成了"自相矛盾"：

```
metrics.targets=1 与 samples=8 不一致
```

查真实样本才发现，**`log_scan` 的 `targets/ok/failed` 是按「日志源」计的，
而 `samples` 是按「日志模板」计的** —— 一个日志源可以产出 9 个模板样本。
其余三个任务则是一一对应。

这属于"契约只存在于大家脑子里"的典型问题。现在校验器按任务分别校验，
并有专门的单测锁住这个差异（`test_log_scan_targets_counts_sources_not_templates`），
免得以后有人"顺手统一"而弄坏夹具。

**教训**：写测试数据的过程本身就在审计接口设计。这个不一致在 225 个单元测试里
一直没被发现，因为它只在"有人试图统一描述它"的时候才暴露。

### H4. 题目的平衡性比数量重要

黄金集 19 个场景，其中 **7 个是"不该告警"的反向用例**：

| 反向题型 | 考什么 |
|---|---|
| 三目标全正常 | 基线：别无中生有 |
| 轻微延迟（未达阈值） | 别把正常抖动当故障 |
| 冷启动样本不足 | 别在没基线时报警 |
| 契约 v1 全通过 | 别把"跑通"也说成问题 |
| 日志模板量级平稳 | 别把日志噪声当异常 |
| 心跳全部按时 | 别把调度正常当异常 |
| **新出现的 INFO 模板** ✱ | **规则层会误报，正确答案是不告警** |

只有正向题的黄金集，一个永远返回 `alert` 的模型能拿满分 —— 这是评测最常见
也最致命的缺陷。所以 `test_has_both_positive_and_negative_cases` 把它变成硬约束。

✱ 那个带星号的是**唯一能区分"AI 有没有独立思考"的题型**：规则层按
"0 → 12 次 = 无法计算倍数"判了 `count_spike`（这个判断会原样出现在喂给模型的输入里），
而正确答案是不告警 —— 因为那是一条 INFO 级的部署日志，不是错误。
**只会复述规则层的模型在这题上必错。**
单测 `test_has_a_rule_layer_false_positive_case` 保证这类题不会被删掉。

### H5. 指标口径

* **混淆矩阵**（`should_alert` 的 tp/fp/fn/tn）→ 精确率 / 召回率 / F1 / 准确率
* **分母为 0 时返回 `None` 而不是 1.0**：没有正预测时"精确率"是未定义的，
  报成 100% 会骗人。这条有单测。
* **`root_accuracy` 是关键词命中率**，不是语义评分。局限写在报告里，
  不假装它是准确率。
* **token 与耗时**一并采集 —— "更准"必须带上"多贵"，否则结论没有决策价值。

### H6. 已知局限（写在报告里，不藏着）

1. `root_accuracy` 是关键词近似，语义质量需要 LLM-as-judge 或人工标注
2. 19 个场景，单个翻转就会让百分比跳动 ±5pp —— 不要拿小数点后的差异当结论
3. "规则层误报"题型目前只有 1 个，应该继续补
4. 规则层这一臂在 `should_alert` 上天然占优（夹具的 anomalies 与
   should_alert 高度相关，因为那正是生产逻辑）；它真正的弱项在**根因表述**上

**知道并写出自己评测的局限，比报一个漂亮的分数更接近工程。**

### H7. 实测结果，以及一个只有评测才能发现的发现

同一批冻结夹具跑了两次全量。**第二次的价值远大于第一次**：

| 指标 | 规则层（同子集 17） | AI 第 1 次 | AI 第 2 次 |
|---|---|---|---|
| `should_alert` 精确率 | 91.7% | 100.0% | 91.7% |
| `should_alert` 召回率 | 100.0% | 100.0% | 100.0% |
| 严重级别准确率 | 100.0% | 100.0% | 100.0% |
| **根因关键词命中率** | **64.3%** | **100.0%** | **100.0%** |

成本：平均 21.0s/次；输入约 6.6k、输出约 3.5k、cache 读约 68k tokens/次。

**分数为什么不一样：** 两次运行的唯一分歧是 `ls_06_new_info_template_benign`。
第 1 次它判「不告警」（对），第 2 次判「告警」（错）——而第 2 次的
`confidence` 只有 **0.55**，且它的 `plain_summary` 里自己写着
"同类没有任何错误……不是故障"。**推理是对的，动作标记是错的。**

单独把这道题重复跑 5 次：**5/5 全部答对**。所以差异可能与批次上下文有关，
也可能只是采样波动 —— **当前样本量不足以区分这两种解释**，这一点必须写出来。

### H8. 从这次评测学到的三件事

**1. 只有稳定复现的提升才值得声称。**
两次运行里唯一都成立的结论是「根因命中率 64.3% → 100%」。
`should_alert` 上 AI 从 100% 掉到 91.7%，所以正确的说法是
"AI 让误报率**下降**"，而不是"AI 让误报率归零"。
如果只跑了一次就发结论，我会把一次幸运当成能力。

**2. 工具的统计口径出错，比没有统计更危险。**
第一版 `--repeat` 之前，token 累加表的键写成了 `in`/`out`，
而 usage 字典的键是 `input_tokens`/`output_tokens` —— 17 次真实调用统计出
**0 成本**。报告上看着"零消耗"，实际上统计彻底失效。
成本数据出错会直接导致错误的决策（"这层 AI 不花钱，多用点"）。
现在有 `test_accumulates_real_usage_shape` 等 4 条单测锁住它。

**3. 评测必须尊重"生产上到底用不用 AI"。**
第一版无脑对每个任务都调用判断层，直接因为缺 `heartbeat.md` 崩溃 ——
而 `heartbeat` 的判据是确定的时间差，生产上**根本不用 AI**。
连带引出一个方法论问题：判断层只覆盖 17/19 个场景，
那么对照臂也必须限制在同一子集上，否则两边 n 不同，差值是假的。
现在报告里同时给出"规则层（全部 19）"与"规则层（与 AI 同子集 17）"。

### H9. 下一步（由这次评测的发现驱动，而不是拍脑袋）

1. **首行加"推理与动作必须一致"的硬约束**：既然第 2 次的失败是
   "分析结论说不是故障、`should_alert` 却为 true"，那就在提示词里明确要求
   "若结论是无影响，`should_alert` 必须为 false"，然后用评测验证是否
   消除了这类不一致（**而不是凭感觉说"我加了一句，应该好了"**）。
2. **把 `--repeat` 纳入常规流程**：单次跑分不可作为结论，需要重复 3~5 次看均值。
3. **扩充"规则层误报"题型**：目前只有 1 个，而它恰好是最能体现判断层独立价值、
   也最不稳定的题型。样本太少，分数才会被单个翻转主导。

---

## 附录 I：给判断层工具（从"判断者"到"调查者"）

### I1. 动手前先查清了工具面

用 `dsh --profile headless --dump-config` 把判断层的插件清单 dump 出来，
得到一个**改变设计前提**的事实：

```
tool-pwsh / tool-fs / tool-fs-search / tool-web / tool-subagent / tool-workflow …
tools.config.mode: !!js process.env.DSH_TOOLS_MODE
```

**判断层本来就有 shell、文件读写、搜索、网络工具。** 也就是说，
我提示词里那句"不要访问网络、不要读日志文件"**从来就不是安全边界** ——
它只是一句请求，模型完全可以不理。

顺带用非法值探出 `DSH_TOOLS_MODE` 的合法取值是 `native | ptc | both` ——
那是**工具呈现方式**（原生函数调用 / 文本式 / 两者），**不是权限档位**。
所以"把模式调成只读"这条路不存在。

**于是设计目标从"加权限"变成"收窄 + 留痕"**：
不扩展能力，而是给它一组**只能碰已声明对象**的只读动作，并加上预算与审计。

### I2. 四个工具，边界都靠"按名字取"实现

| 工具 | 收窄手段 |
|---|---|
| `read_log` | 只接受 `[[log_scan.sources]]` 里的**名字**，把名字映射成路径。**传路径直接拒绝** —— 路径穿越从根上不存在 |
| `probe_target` | 只接受 `[[api_watch.targets]]` 里的**名字**。否则它能输入任意主机名，把这台机器当跳板扫内网 |
| `query_history` | 只读连接，SQLite 层拒绝写入 |
| `read_report` | 校验 run_id 形状 + 路径必须落在报告目录内（双重检查） |

**"按名字取"这个选择是刻意的**：如果接受路径，就得写一大堆路径规范化/白名单前缀
逻辑来防穿越，而且每次都要担心绕过。改成"名字必须先出现在配置里"，
攻击面直接归零 —— **能碰什么是配置决定的，不是输入决定的。**

### I3. 两个把工具做坏的地方，都是"实测才发现"的

**（1）`history` 在沙箱里打不开数据库。**
判断层第一次真实调用工具时，`catalog` 正常而 `history` 报
`OperationalError: unable to open database file`。
根因：库是 WAL 模式，缺 `-wal`/`-shm` 附属文件时只读连接需要能创建共享内存文件，
沙箱里做不到。修法是**两条路都保留**：先试 `mode=ro`（最硬），
失败退到普通连接 + `PRAGMA query_only=ON`（写入同样被 SQLite 拒绝）。
**只读这条性质一点没丢**，丢的是"只读"的实现方式 —— 有单测同时盯住两条路。

**（2）`read_log` 会把整个日志读进内存。**
第一版是 `read_text().splitlines()` 再取尾部。生产日志动辄几百 MB，
整读一次既吃内存又可能卡住判断层（工具没有硬超时）。
改成从文件末尾往回读一块**有上界的字节**再按行切：内存占用与文件大小无关，
**实测 11.2 MB 日志 0.020 秒返回**；而且当只读了尾部时**明确标注"总行数未知"**，
不假装精确。

顺带踩了一个经典坑：新函数命名成 `tail_lines`，和 `read_log` 的同名参数互相遮蔽，
直接 `TypeError: 'int' object is not callable`。改名 `read_tail`。

### I4. MCP 服务端：写完了，但说清为什么挂不上

`ops/investigate/mcp.py` 是**纯标准库手写的 MCP 服务端**：
JSON-RPC 2.0 over stdio，实现 `initialize` / `tools/list` / `tools/call` / `ping`，
通知不产生响应，坏 JSON 回 `-32700` 而不是崩掉。
协议完整性由一个真的 stdio 测试客户端验证（多行输入、逐行应答、坏 JSON 混在中间）。

**为什么今天挂不进流水线**：`dsh headless` 的 profile 里没有 MCP 客户端的配置面
（`--dump-config` 逐项确认过，只有 `dsh-mcp-resources` —— MCP *资源*，不是服务器连接）。
MCP 服务器走的是 ACP 的 `session/new` `mcpServers`，由**客户端**传入
（asar 里能看到 `SessionBuilder.withMcpServer`），而 `dsh headless` 自己就是客户端。

所以做了**同源的第二条通道**：`tools/investigate.py`（CLI），
判断层用它已经有的 `pwsh` 调用。**两者共用 `ops/investigate/tools.py` 这一份实现**，
所以不存在"两条通道行为不一致"。MCP 服务端留着 ——
它对任何 MCP 客户端都可用，是这套能力更通用的出口。

### I5. 预算：把"拦得住"和"看得见"拆开（本附录最重要的一条）

工具调用预算 6 次/运行。第一次真实调查时它**静默失效了**：

```
[catalog]   --- 工具预算：还可以调用 6 次（已用 0/6）
[history]   --- 工具预算：还可以调用 6 次（已用 0/6）
[probe]     --- 工具预算：还可以调用 6 次（已用 0/6）
```

五次调用，每次都"已用 0"。根因有两层：

1. **判断层的沙箱 shell 只能读、写不进状态文件**
   （`SetNamedSecurityInfoW failed (Win32 5): grantWrite(D:\AITool\ops-pipeline)`）；
2. **我的代码把写入异常吞了** —— `except OSError: pass`。

第二层是我的错，而且正是这个项目一直在对抗的那类错误：**静默失败**。
一个静默失效的预算比没有预算更危险 —— 你会以为有保护。

修法分三步，**关键是承认这两件事不是一回事**：

| 角色 | 谁做 | 有效性 |
|---|---|---|
| **强制**：拦住第 7 次 | CLI 写状态计数器 | 需要状态目录可写 |
| **检测**：看得见超了 | 宿主侧从 `events.ndjson` 数 `tool_call` | **任何环境都有效**，零写入 |

配套：CLI 写入失败**显式报错**不再吞；`catalog` 自带**落盘自检**
（直接回答"计数能不能写、当前运行标识是什么"）；宿主侧独立计数写进报告附注。

实测宿主侧计数（免费，直接读事件流）：某次运行 **12 次工具调用，其中 6 次是调查工具**，
逐条命令都能列出来。**"拦不住 ≠ 看不见"** ——
把这两件事混为一谈，是这类护栏最常见的自欺。

### I6. 顺带发现的另一个问题：ACL 与判断层的 shell

判断层第一次要调工具时失败于 `grantWrite(D:\AITool\ops-pipeline)`。
按 `diagnose-windows-sandbox-acl` 技能做了一次修复：该目录原先缺少 `WRITE_OWNER`，
补上当前用户的完全控制项后复查通过（`writeOwner: true`，改动前有备份、有回滚命令）。

**但判断层的 shell 仍然不能写**（能读、能跑读操作，写状态文件失败）。
按技能要求"重复原操作受限运行"后确认：这是**更深一层的原因**，不是同一次 ACL 缺失。
技能明确指出这种情况要停下来、把阻塞对象和证据交给用户，而不是继续反复尝试修复 ——
所以这里如实记录：**读取路径已完全打通并在生产上验证过；
写入路径在该环境下受限**，预算因此采用"检测为主、强制为辅"的设计（见 I5）。

### I7. 实测：一次真实的七步调查

给判断层这批工具后，它在一次**真实生产巡检**里自己走完了这条链：

```
catalog                                   → 拿到允许访问的对象
history --task api_watch --days 7         → 查这个目标过去有没有成功过
probe --target worldtime                  → 立刻重探，确认"现在仍然失败"
history --task api_watch --target frankfurter --days 7
read-log --pattern "worldtime|URLError|10054|ERROR"   → 去日志里找对应报错
report --run-id <历史 run>                → 读上次同类问题的结论
write verdict → 自己用 shell 校验 JSON → 发现格式错 → edit 修好 → 复验
```

**最后那步尤其说明问题**：它自己写了 JSON、自己校验、发现不合法、自己修好、再验一次。
这不是"一次问答"，而是一个**带反馈闭环的循环** —— 正是"调查者"与"判断者"的区别。

---

## 附录 J：人工反馈回路

### J1. 为什么离线评测不够

附录 H 建了黄金集，能回答"模型在 19 个场景上多准"。但它回答不了：
**它在你这儿多准。**

真实环境里的误报往往长得跟黄金集里的不一样。构建黄金集时我能想到的题型有限，
而现实会送来我没想到的。所以需要一条**从真实使用中收集反例**的通路。

### J2. 交互方式的约束推导

最自然的交互是邮件里放两个按钮，点一下打上分。**但那需要一个从手机可达的地址。**

家宽没有公网 IP、没有域名。链接点下去只会打到手机自己。
这和附录 E6 里企业微信自建应用撞的是**同一堵墙** —— 当时卡在"接收消息服务器URL 需要公网可达"。

**回复邮件不需要任何可达地址**：邮件出去、回复回来，中间不需要你的机器能被访问。
代价是要轮询邮箱（`feedback_scan` 任务），但那是纯本地行为。

> 这是这轮唯一"没被环境挡住"的设计，因为它的方向反过来了：
> 不是让外界来找你，而是你主动去取。

### J3. 三个刻意的取舍

| 决策 | 选择 | 理由 |
|---|---|---|
| 覆盖 vs 追加 | 同 (run_id, source) **UPSERT** | 人改主意是正常的；累计两次会把统计弄脏 |
| 认不出来 | **静默跳过**，不算错误 | 自动回复、签名、无关邮件是常态；猜错比不认更糟 |
| 关键词顺序 | **先判负向** | `"不对"` 含 `"对"` |

第三条是这类匹配最经典的坑，有专门的单测（`test_down_checked_before_up`）。
**把误报记成"判对了"比没有反馈更糟**：准确率虚高，正好掩盖真问题。

另外两条不算显然的：

* **引用正文必须跳过**。多数客户端回复时会把原邮件整段带上，
  而原邮件里有 `plain_summary`、`alert` 这类词 —— 不跳过就可能把引文当反馈。
  实测的判据：`>` 开头的行、以及"写道：/发件人:/From:"这类分隔行**之后**的内容全部丢弃。
* **评测产生的 run 不能被认领**。`find_run_by_token` 显式排除 `eval-%` ——
  那些不是真实告警，被人工反馈认领会污染统计。

### J4. 诚实地标注采样偏差

`stats` 打印的准确率**天生有偏**：你更可能去反馈判错的那几条。
所以 CLI 输出、日报、README 里都写着同一句话 ——

> 这个数字只用来看趋势、找反例，不能当成模型在全部告警上的准确率。

**一个不说明采样偏差的准确率是误导**，哪怕算法本身完全正确。

它的真正用途是**收集反例**：判错的那几条正好是黄金集缺的题型。
这就把附录 H 的离线评测和真实使用连成了一个闭环 ——
离线评测保证不下滑，人工反馈提供新的提升方向。

---

## 附录 K：配对实测 —— 加了工具，这次没有变准

### K1. 实验设计：配对，而不是两次独立的运行

`tools/eval_suite.py --tools` 在**同一次运行**里对每个场景跑两臂（无工具 / 有工具）。
为什么必须配对：

* 两次独立运行的分数本身会抖（附录 H 已实测：同一批夹具两次全量差 8pp）；
* 工具臂不是 hermetic 的（工具面与工具结果都是活的），跨运行比较会把这层噪声叠上去。

配对之后，"有没有工具"是**同一批夹具、同一个会话策略、同一时刻**下的唯一变量。

### K2. 结果：决策指标完全一致

| 指标 | 规则层 (n=17) | AI 无工具 | AI + 只读工具 |
|---|---|---|---|
| `should_alert` 精确率 | 91.7% | 100.0% | 100.0% |
| `should_alert` 召回率 | 100.0% | 90.9% | 90.9% |
| 严重级别准确率 | 100.0% | 100.0% | 100.0% |
| 根因关键词命中率 | 64.3% | 92.9% | 100.0% |
| 输入 tokens | — | 152,882 | 125,671 |
| cache 读 tokens | — | 1,547,264 | 1,049,344 |
| 平均耗时 | — | 33.9s | 27.8s |

**两臂各错一个场景，正好抵消。** 根因命中率的 +7.1pp 是**一个场景的翻转**（n=14）。

成本那个 −18%/−32% 看起来诱人，但**只有一次运行**：
附录 H 已经证明同一批输入重复跑分数会抖，单次 18% 完全可能是噪声。
**要主张成本差异就必须重复跑**，否则就是拿噪声当发现。

### K3. 为什么测不出好处：题目不需要调查

这才是本次实验最有价值的产出。逐个场景看"有工具臂是否真的调了工具"：

| 场景 | 工具调用 |
|---|---|
| aw_01 / aw_04 / aw_06 / ls_01 / rg_01–rg_04 | **0 次** |
| aw_02 / aw_07 | 2 次 |
| aw_03 / aw_05 / ls_02–ls_05 | 3–4 次 |
| ls_06 | **6 次（用满预算）** |

模型**只在能改变结论时才查**：异常在场就查，输入已经足够就跳过。
这个行为本身完全正确 —— 提示词里也是这么要求的。

问题出在题目上：**19 个夹具全都是自证的。**
`run_doc` 里 `samples` / `baseline` / `anomalies` 一应俱全，答案都在输入里。
**没有一道题必须去查日志或历史才能答对。** 既然如此，工具当然测不出增量。

> **评测只能衡量它的题目所要求的能力。**
> 想衡量"调查能力"，就得有"证据不足、必须自己去查"的题目 —— 那正是黄金集现在缺的一类。

### K4. 两个"错"是校准分歧，而且可能是我标签太严

两臂各错的那一个，**都不是推理错误**：

| 场景 | 模型结论 | 我的标签 |
|---|---|---|
| `aw_05` 延迟 6 倍（单样本） | `notice`，"一次性抖动，可能对方网络不稳"，confidence 0.62 | 期望告警 |
| `aw_07` 结构变更 | `notice`，"看起来是改了返回格式"，confidence 0.75 | 期望告警 |

**它认出了根因，只是不肯为此叫醒你。** 这是校准分歧，不是能力缺陷 ——
甚至可能是我的标签太严：单样本 6 倍延迟，一个老练的运维确实会先观察一轮。

**但我没有改标签。** 看到模型答案之后再改题，就是把测试集往模型上拟合，
那种"改进"毫无意义。要改必须有**独立判据**，例如先写下明确策略：

> p95 超过基线 3 倍、且**连续两轮**成立 → 告警；单轮 → notice。

然后用这条策略重标所有场景，并重新跑一遍确认。那是一次真正的迭代，不是事后调答案。

顺带一个更深的启示：`recall` 现在度量的一部分是"**模型是否和我一样保守**"，
而不是"能不能发现问题"。把作者的风险偏好混进指标里，是这类标注集最常见的隐性偏差。

### K5. 下一步（由结果驱动，不是拍脑袋）

1. **补"证据不足"题型**：把部分场景的 `baseline` / 历史**拿掉**，只留当前一轮。
   这时"是新加的目标"和"是长期故障"在输入里无法区分，**必须**查 `history`。
   这才是能体现工具价值的题型，也是 K3 的直接结论。
2. **先定阈值策略、再重标标签**（见 K4），让指标度量能力而不是一致性。
3. **要主张成本差异就重复跑**（`--repeat 3`），单次 18% 不作数。
4. 工具调用的**审计与预算**按附录 I5 的结论办：强制靠 CLI（需状态目录可写），
   检测靠宿主侧事件流（任何环境都准）。

### K6. 这次实验本身值多少

一次"零结果"如果能说明**为什么零**，它的价值不低于一次正向结果：

* 它把"我加了工具，感觉更好了"这种**无法证伪的说法**换成了一个可复现的对照；
* 它指出**评测题目本身的设计缺陷**（全自证 → 测不出调查能力），
  而这个缺陷在只看总分时是看不见的；
* 它暴露了**标签里掺进了作者的风险偏好**，这是一个更隐蔽、也更容易误导的偏差。

**如果只跑一次、只看总分，这三件事一件都不会被发现。**

---

## 附录 L：三个加分项

### L1. 小模型分流：把"需要多少判断"变成一个可配的先验

**先查清了模型面**：用非法模型名触发校验错误，逼 DSH 把可选项列出来 ——

```
The supported API model names are deepseek-flash, deepseek-v4-pro
```

一便宜一强，分流才有意义。`dsh headless` **没有 `--model`**，所以走 **patch 组合**：
每个模型一个固定的 patch 文件，拼在 `ai.patch` 之后（patch 层是"后来者覆盖"）。
这一点用 `--dump-config` 验证过：组合后的配置里确实是 `deepseek-v4-pro` ——
**不验证就会得到"三个臂其实跑同一个模型"的无效实验。**

**判据的核心不是"异常多不多"，而是"要不要在证据之间权衡"**：

| 类型 | 例子 | 走哪个模型 |
|---|---|---|
| 机械型 | 日志源不见了、心跳发现任务没跑、整轮采到 0 条 | 便宜模型（事实即结论） |
| 解释型 | 200 但内容变了、延迟涨 6 倍但只有单样本、冒出新错误模板 | 强模型（要权衡） |

外加两条升级条件：异常项 ≥ 2、失败目标 ≥ 2（多股证据要合并判断）。
**拿不准就升级** —— 分流的收益是成本，代价是潜在准确率，所以先验宁可保守。

实现上有三个刻意的选择：

1. **`ai.model` 写死时绕过分流**：评测要能分别跑"全便宜"和"全强"两臂，
   没有这个开关就没法做对照实验。
2. **每轮的选择理由落库**（`ai_notes`）：事后要能回答"这次用了哪个、为什么"。
3. **patch 写不进去就退化为 `None`**（用 profile 默认模型）——
   分流失败**绝不能**让判断层整体失败。这条有专门的单测，而且踩过一次：
   `ensure_dir` 一开始写在 try 之外，于是"状态目录建不出来"会直接抛异常。

**这个先验不是结论。** `[ai.routing]` 里每个阈值都可配，存在的意义就是让人拿评测去改它。

### L2. 多视角诊断：让三个镜头各自说话

单次判断里，模型要在**同一次思考**中同时回答三个性质不同的问题：

1. 目标自身的行为变了吗？（接口/契约镜头）
2. 日志里有对应证据吗？（日志证据镜头）
3. 这是新问题还是反复出现的？（历史趋势镜头）

它们互相干扰 —— 模型容易抓住**最显眼**的那条线索就下结论，
而最显眼的不一定最相关：一个延迟飙升的错误簇，真实原因可能在三天前的历史里。

所以启用后，判断层**先并行派子代理分头查证，再汇总**，并被要求逐条标明
结论来自哪个镜头（`[历史镜头] …`）。

**为什么放在数据里而不是提示词里**：`run_doc` 里多一个 `multi_perspective` 块，
提示词只静态描述这套契约。这样"这次要不要分镜头"由**这一轮的事实**决定，
评测也就能靠注入/不注入这个块来跑对照臂 —— 不必维护两份提示词。

**⚠️ 降级路径必须写在块里**：派不出子代理（工具不可用、编排失败）时，
要**照常按单次判断给结论**，并在 `evidence` 里写明"多视角编排未生效"。
**绝不能让没发生过的编排出现在报告里** —— 那是最坏的一种幻觉：
不是结论错，而是**过程在撒谎**。这句话在配置、提示词、块里各写了一遍，
并有单测盯着。

触发条件与分流的升级条件**故意接近**（都看"是否需要权衡证据"），
但两者是独立开关：可以"强模型 + 单镜头"，也可以"便宜模型 + 多镜头"，
用评测分别量它们各自的性价比。

### L3. 只读面板：把已有的东西摆到一起

四条约束，每条都有理由：

| 约束 | 理由 |
|---|---|
| **零前端依赖**（内联样式的单页） | 项目一路坚持零第三方依赖，面板不该是第一个例外 |
| **只绑 `127.0.0.1`** | 它读的是你的运维数据（含告警详情），不该出现在局域网上 |
| **只读**（全部查询走 `mode=ro`） | 一个能触发动作的面板就是一个新的攻击面 |
| **没有它也能用** | 报告是 markdown、数据在 SQLite、告警在邮件里；面板不是新的真相来源 |

首屏第一块是**"需要你注意的事"**，空闲时明确写"没有"。
运维面板要回答的是"**有东西坏了吗**"，不是"欢迎使用运维平台"。

两个实现细节值得记：

* **`eval` 与 `doctor` 两块不依赖历史库**，所以放在最前面算 ——
  一台还没跑过任何任务的机器也应该能看到自检结果。第一版把它们放在
  "库存在"的分支之后，于是新机器上什么都看不到。
* **HTML 必须转义**：面板渲染的是日志与告警内容，而日志是**外部输入**
  （写日志的人不一定是你）。`<script>` 或 `<img onerror=…>` 完全可能出现在
  一条日志里。有单测专门喂这两种内容进去断言被转义。

### L4. 顺带补上的一个系统性缺口

写面板时我又踩了自己的老坑：`dashboard.py` 忘了在最开头调 `make_console_safe()`，
于是 HTML 里的 `⚠️` 把 GBK 控制台打崩了。

这个项目一开始就为它做过根因修复，也留了测试 —— **但那个测试只覆盖
`python -m ops`，没覆盖 `tools/` 下的脚本。** 所以后来新写的脚本又踩了一遍。

于是补了一条**系统性**的测试：扫所有入口脚本，用 AST 检查它们是否调用了
`make_console_safe()`。它上线立刻又抓出 4 个漏掉的脚本（包括会打印中文表格的
`eval_suite.py`）。

> **靠人记住"新脚本要加一行"是靠不住的；让测试去扫，才叫系统性。**

### L5. 这三项的共同点

它们都不是"再加一个功能"，而是**在同一个判断链路上换不同的资源组合**：

* 分流换的是**模型**（便宜/强）；
* 多视角换的是**上下文组织方式**（单次思考/分头查证）；
* 面板换的是**人怎么看见**（翻文件/一屏）。

三者的效果都必须用同一套评测脚手架量 —— 这也正是前面几轮先把评测建起来的回报：
**没有评测，这三个加分项只能靠"我感觉更好了"来交付。**

---

## 附录 M：接一条新流水线（price_watch）

这是对整套架构**"骨架能不能复用"**的一次真实检验 —— 不是嘴上说可扩展，
而是真的接一个新场景，看要改多少东西。

### 复用与改动

| 组件 | 复用了？ |
|---|---|
| 落库（`sample` 表，`metric='price'`）、`run`/`verdict`/`alert` | ✅ 一行没改 |
| `pipeline.finalize`（去重/冷却/静默时段/发信/恢复通知/落盘） | ✅ 一行没改 |
| 邮件 / 企业微信 / ntfy 通道 | ✅ 一行没改 |
| 只读调查工具（白名单 + 预算 + 输出截断） | ✅ 加了**一个**工具 |
| 小模型分流 / 多视角诊断 | ✅ 零改动即可用（都基于 `analysis.anomalies`） |
| 评测脚手架 | ✅ 加了 5 个场景 |
| 采集 | ❌ 新写（配置驱动） |
| 判据 | ❌ 新写（价格语义） |
| 报告渲染 | ⚠️ 加了两个版式回调 |

收尾逻辑只写一份的回报在这里兑现了：新流水线**不用再实现一遍**
"发不发、发过没有、发失败了怎么办、恢复了要不要说"。那部分是这个项目里
最容易写错的代码，而它现在只存在一份。

### 设计上真正的差别：价格没有"正常值"

`api_watch` 的判据是"偏离基线 N 倍"，因为"接口应该在 200ms 内响应"是有意义的。
**价格不是。** 显卡卖 4000 还是 4300 都叫正常。

所以价格只用三个有明确含义的判据：

| 判据 | 依据 | 为什么它有意义 |
|---|---|---|
| 到价 | **用户自己设的**目标价 | 唯一真正确定的"我想要的价格" |
| 单次涨跌幅 | 相对**上一次** | 一夜降 8% 是事件；长期在 4000±100 波动不是 |
| 窗口极值 | 30 天内最低 | "创 N 天新低"是人真正会行动的信号 |

**刻意不用"偏离中位数 N 倍"** —— 那需要先假设价格有个基准。
把它当 `api_watch` 调参，结果只会在促销季疯狂误报。

第二个差别是**语义方向**：故障告警说"出事了"，价格告警说"可以买了"。
所以价格异常走 `notice`，提示词里明确要求"语气是机会不是事故"。

### 抓到的两个真 bug

**① 本轮样本污染了"上一次价格"**（这个更严重）

`api_watch` 里有一段刻意为之的顺序：**先写入本轮样本、再统计连续失败次数**，
否则"第 3 次连续失败"会被算成第 2 次。我照抄了这个顺序。

但价格判据要的是"**相对上一次**的变化幅度"，而"上一次"在本轮样本已经落库之后
就变成了**本轮自己** —— 于是 `(现价 - 上次价)` 恒等于 0，
**`price_drop` 永远不会触发**。不报错、不变慢，只是安静地什么都不做。

它当初"看起来能用"，是因为我的两次验证运行落在同一秒、而 `ts` 只有秒级精度，
SQLite 恰好返回了另一行。**是运气，不是设计。**

> **教训**：可复用的顺序不等于可复用的语义。
> `api_watch` 要"含本轮"，价格要"不含本轮" —— **同一个表，两个场景的基线定义相反。**
> 修法是 `price_history(exclude_run_id=...)`，并补了一条用**同一个 ts**
> 构造最苛刻情形的回归测试。
>
> 顺带修了秒级精度：`ORDER BY ts, rowid` 兜底。

**② `JsonResponse.ok` 的意思是"有响应"，不是"成功取到"**

它的设计目的是给契约断言层用：4xx/5xx 也算"有响应"，而且断言层**必须看到状态码**。
价格采集拿这个字段判断，就会把一个 404 的响应体当成有效数据继续往下走，
最后报出一个莫名其妙的"路径断了" —— **错误信息指向了错误的地方**。

修法是在价格采集里要求 2xx。两个 bug 都补了回归测试。

### 顺带泛化了共用渲染器

第一版价格报告长这样：

```
| rtx5070 | OK | 3599.0ms |          ← 价格被加了毫秒后缀
| 商品 | 基线样本 | p50 | p95 | 连续失败 | 24h 失败 |   ← 对价格无意义的列
| rtx5070 | 3 | Nonems | Nonems | None | None |
```

渲染器写死了探测语义。修法是加两个**版式回调**：

* `value_format(sample) -> str` —— 值怎么显示
* `baseline_columns` —— 基线表有哪些列

而"价格怎么显示"留在**价格模块**里（`prices.report_style()`）。
这样渲染器保持通用（它不该知道"价格"这个概念），
下一个场景接进来时也不用再改它一次。

### 采集为什么是配置驱动

最初的想法是爬电商页面。**不做。** 那类页面有反爬、有 ToS 限制、改版就失效 ——
把系统绑在一个随时会坏的抓取上，是这类项目最常见的死法。

所以只提供"**给定 JSON 地址 + 取值路径就能用**"的能力：
接官方 API、接比价服务、接你自己写的一层中间层都行。
配套的 `tools/price_stub.py` 是个本地桩，让验证完全不依赖外部站点
（既有反爬就有 ToS 问题，而且对方一改版测试就跟着坏）。

### 实测：判断层给出的那封邮件

让价格 4599 → 3499，判断层（用 `check-price` 亲自复核之后）写了：

> **RTX 5070 到你的目标价了，现在 3499 元**
>
> 已经低于你设的 4000 目标价；从 4599 一路降到 3599，现在又降到 3499，
> 总共便宜了 1100 元左右……另外两个显卡价格没变，不用管。

它还主动做了三件对的事：`check-price` 复核**排除单次采集抖动**、
`history` 确认**没有更早的历史**所以不下"历史新低"的断言、
指出前一轮告警**因静默时段被抑制**（而不是假装通知过）。

**这三件事都不是"更聪明"，而是"按要求把能查的查了、并且如实说不知道"** ——
正是前面几轮把工具面、降级契约、冷启动保护都写进提示词换来的。



