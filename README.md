# ops-pipeline

个人质量保障与运维流水线：**确定性脚本负责采集，AI 负责判断，脚本再决定要不要打扰你。**

用 DeepSeek Harness 的 `headless` 模式做判断层，用 Windows 任务计划程序做触发，
用 SQLite 存历史，用企业微信发告警。**零第三方依赖**，只用 Python 标准库。

> **项目位置**：正本在 `D:\AITool\ops-pipeline`。桌面上的 `ops-pipeline` 是指向这里的
> **快捷方式**，不是第二份拷贝 —— 刻意只保留一份，避免两份代码各自演化后分叉。

---

## 为什么不用 cron + 一个脚本

cron 能做到"每天早上 8 点跑脚本"。但它做不到这些：

- 7 个接口用例挂了 3 个 —— **是同一个根因还是三个独立回归？**
- 日志里冒出一个以前没见过的错误簇 —— **是新问题，还是已知噪声？**
- 昨天同一时间也挂过一次 —— **这是偶发还是趋势？**
- 结论要写成人能读的报告 —— **哪句是事实、哪句是推测？**

这四件事需要判断。**这才是这套东西存在的理由**：cron 管"什么时候跑"，AI 管"跑出来的东西意味着什么"。

---

## 四条流水线

| 任务 | 频率 | 干什么 | 判断层要回答的问题 |
|---|---|---|---|
| `api_watch` | 每小时 | 探测外部接口的健康与延迟，与 30 天基线比对 | 这次失败是上游故障、结构变更，还是网络抖动？ |
| `regression` | 每天 08:30 | 对自己的接口跑 7 条契约用例（自带靶子服务） | 这几个失败是同一个根因，还是几个独立回归？ |
| `log_scan` | 每天 09:00 | 把日志聚成模板，比对两个窗口的量级 | 哪个错误簇是**新出现**的、哪个**量级炸了**，同源吗？ |
| `daily_report` | 每天 09:30 | 汇总 24h 的运行/告警/token，写一份日报 | 这一天的整体情况怎么样，有什么该关注的？ |
| `heartbeat` | 每天 23:00 | 用**独立机制**检查"预期该跑的 run 有没有跑" | （不用 AI：判据是确定的时间差） |

---

## 架构

```
┌─ 触发层 ──────────────────────────────────────────────┐
│  Windows 任务计划程序                                  │
│   每小时    → api_watch    接口可用性巡检              │
│   每天 08:30 → regression  接口回归 + 归因 + 晨报       │
│   每天 09:00 → log_scan    日志模板聚类 + 归因 + 简报    │
│   每天 09:30 → daily_report 汇总日报（固定会话，有记忆）  │
│   每天 23:00 → heartbeat   检查"预期该跑的跑了没"       │
└─────────────────────┬────────────────────────────────┘
                      │  python -m ops --task X --once
┌─ 执行层（确定性 Python，绝不调用 LLM）───────────────┐
│  采集/跑用例 → 落库 → 基线比对 → 规则判定 → run.json   │
└─────────────────────┬────────────────────────────────┘
                      │  run.json（文件契约）
┌─ 判断层（dsh --profile headless）───────────────────┐
│  读事实 → 归因 → 分级 → 写 verdict.json + 报告         │
└─────────────────────┬────────────────────────────────┘
                      │  verdict.json（文件契约）
┌─ 裁决与告警层（core/pipeline.py，所有任务共用）─────┐
│  读结论 → 去重/冷却/静默 → 发企业微信 → 落库 → 报告     │
└───────────────────────────────────────────────────────┘
```

### 三条铁律

1. **确定性的活给脚本，判断性的活给 AI。**
   网络请求、断言、算分位数、失败分组是脚本的活；归因、分级、写报告是 AI 的活。
   反过来做（让 AI 每次重新生成测试/爬虫代码）会得到不稳定、难复现、还烧 token 的系统。

2. **AI 只能读写文件契约，不能直接发告警。**
   AI 产出 `verdict.json`，由脚本决定发不发。于是告警逻辑可测试、可审计、可限流，
   **AI 的幻觉不会直接炸你的微信**。同时也解决了无人值守的权限问题：
   判断层不碰网络和数据库，就不存在"需要人批准的操作用户不在场"导致任务失败。

3. **执行层不依赖 AI 就能跑通。**
   把 `ai.enabled` 关掉，整套流水线照常工作，只是结论标注为"规则层兜底、未经 AI 分析"。

### 第四条（实现时加的）：收尾逻辑只有一份

判定 → 报告 → 裁决 → 告警 → 落库 这段逻辑在 `core/pipeline.py` 里，
三条流水线共用。**复制一份的代价**是：冷却规则、降级标注、幂等键会在两边各自演化，
最后出现"这条流水线的告警被抑制、那条没有"的不一致 —— 那种 bug 极难发现。

---

## 快速开始

```powershell
# 进入项目目录（桌面上的 ops-pipeline 快捷方式也指向这里）
cd D:\AITool\ops-pipeline

# 1) 先跑一次看看（不需要任何凭据，也不调用 AI）
python -m ops --task api_watch --once --no-ai
python -m ops --task regression --once --no-ai --override regression.variant=v1
#    报告写到 data/reports/<本地日期>/

# 2) 验证监控真的抓得到故障（重要，别跳过）
python tools/fault_drill.py --port 8788        # 另开一个终端
#    把 config/config.toml 里 fault_drill 的 enabled 改成 true，连跑 3 次
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8788/fault/on

# 2b) 同样验证日志分析抓得到已知异常（生成器会告诉你注入了多少，好对答案）
python tools/loggen.py --out data/logs/clean.log --hours 48
python tools/loggen.py --out data/logs/app.log --hours 48 --inject new-error --inject spike
python -m ops --task log_scan --once --no-ai --override log_scan.sources.0.path=data/logs/clean.log
python -m ops --task log_scan --once --no-ai --override log_scan.sources.0.path=data/logs/app.log

# 3) 验证 AI 归因（复用你桌面端已登录的 DeepSeek 账号，无需 API Key）
python -m ops --task regression --once --override regression.variant=v2
python -m ops --task log_scan --once --override log_scan.sources.0.path=data/logs/app.log

# 4) 配置告警通道（不配也能跑：告警会落到 data/logs/notify.log）
#    实测四条路之后，email 是唯一没有前置门槛的方案 ——
#    不要实名、不要付费、不经第三方、无额度上限。
#    ① 开 SMTP：登录 mail.qq.com → 设置 → 账户 → 「IMAP/SMTP服务」开启
#       （按提示发一条验证短信，得到 16 位授权码，注意不是 QQ 密码）
scripts\set-secret.cmd -Name OPS_MAIL_USER -Hint "你的QQ邮箱，例如 12345@qq.com"
scripts\set-secret.cmd -Name OPS_MAIL_PASS -Masked -Hint "上面拿到的16位授权码"
scripts\set-secret.cmd -Name OPS_MAIL_TO   -Hint "收件地址（可以就是同一个邮箱）"
python tools\notify_test.py               # 应看到 结果: sent，然后去收件箱确认
#    其它通道（按实测结论排序，详见 DESIGN.md 附录 E6）：
#      ntfy       服务可达，但国内安卓应用商店不让装
#      wecom      群机器人：需要普通内部群，全员群/单人企业拿不到入口
#      wecom_app  自建应用：需已备案域名或公网服务器才能填企业可信IP
#      pushplus   实测要求付费实名认证（code=905）

# 5) 注册计划任务（双击 scripts\register_tasks.cmd 也行）
scripts\register_tasks.cmd -DryRun            # 先预览
scripts\register_tasks.cmd                    # 注册 api_watch / regression / heartbeat
#   log_scan 默认不注册：它监视的日志源还指向演示用的合成日志。
#   把它指向你自己的日志后，加 -IncludeLogScan 一并注册：
#   scripts\register_tasks.cmd -IncludeLogScan
#   移除：scripts\register_tasks.cmd -Remove
```

第 2 步不是可选项：**你必须先证明"监控能抓到已知故障"，才敢相信它在真实故障面前不撒谎。**

### 常用开关

```powershell
python -m ops --list                                   # 列出任务
python -m ops --task regression --once --dry-run       # 不发真告警，落本地日志
python -m ops --task regression --once --no-ai         # 强制规则层兜底
python -m ops --task regression --once --override regression.variant=v1
```

`--override KEY=VALUE` 是为"临时改个参数重跑一次"准备的，不用为了跑一次去改配置文件。

### 企业微信自建应用：三个值到底在哪拿

**关键区别：这三个值不在「企业微信」客户端里，而在「管理后台」网页上。**
客户端是聊天用的；管理后台是另一个地方：

* 浏览器打开 <https://work.weixin.qq.com/> → 点「管理后台」→ 用手机企业微信扫码登录
* 或者：桌面客户端**左上角头像 → 管理企业**，会跳转到同一个后台

登录后按顺序做四件事：

| 步骤 | 位置 | 拿到什么 |
|---|---|---|
| 1 | 顶部「我的企业」→ 左侧「企业信息」→ 拉到**页面最下方** | **企业ID**（形如 `ww1234567890abcdef`）= CorpID |
| 2 | 顶部「应用管理」→「应用」→「自建」→ **创建应用** | 上传任意图标、填个名字（如 `ops-alert`） |
| 3 | 创建时的「可见范围」 | ⚠️ 必须选**你自己**（或整个企业），否则推送会返回 `invaliduser` |
| 4 | 创建后自动进入的**应用详情页** | 直接看到 **AgentId**（纯数字，形如 `1000002`）；**Secret** 点「查看」→ 会发一条消息到企业微信里，点开就能看到 |

四步做完后回到终端：

```powershell
scripts\set-wecom-app.cmd
```

脚本会依次问你这三个值，写入 `config\secrets.local.toml`，然后**立刻发一条测试消息**。

**拿不准某一步时**：`scripts\set-wecom-app.cmd -Show` 看当前配到哪了（值会打码）；
报错里带 `errcode` 时按码查 <https://open.work.weixin.qq.com/devtool/query>。

---

## 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 触发方式 | Windows 任务计划程序调 `--once` | 无状态、可重入、可手动补跑；不需要守护进程 |
| 依赖 | **零第三方依赖**（仅标准库） | 无人值守场景下依赖越少越不容易半夜挂掉 |
| 配置格式 | TOML（`tomllib`，Python 3.11+ 自带） | 要有注释，又不想引入 PyYAML |
| AI 凭据 | `--patch` 切到 `deepseek-account` | 复用桌面端登录，**不用额外申请 API Key** |
| AI 会话 | 巡检用新会话；日报用固定会话 | 每小时一次独立检查共用会话会让上下文无限增长 |
| 用例形式 | 声明式数据 + 断言 DSL | 加用例不用碰运行器；失败明细天然结构化，AI 归因不用解析文本 |
| 回归靶子 | 自带 v1/v2 变体，任务自己拉起又关掉 | 你无法在真实项目里故意弄坏接口；计划任务也不该依赖"你得先手动启动服务" |
| 告警幂等 | 唯一索引 `(dedup_key, run_id)` | 单轮幂等 + 跨轮冷却。若只按 `dedup_key` 唯一，同一条告警永远发不出第二次 |
| dry-run | 计入冷却 | dry-run 必须**忠实模拟**真实发送，否则演练结果误导人 |
| 发送失败 | 不计入冷却 | 失败就该下一轮重试，不能被冷却压住 |
| 连续失败 | **限定 24 小时窗口** | 不设窗口会和 `failures_24h` 自相矛盾（详见下文） |
| 日志基线 | 同一文件里比**两个紧邻窗口**（当前 vs 前一窗口） | 第一轮就有基线，不存在冷启动空转；历史库只作补充 |
| 日志基线污染 | `log_window.anomalous` 标记，**故障期窗口不进基线** | 否则故障量级被算成基线，异常逐渐变成"新常态" |
| "新模板"判据 | 看 `first_seen` **是否在宽限期内**，而非"历史里有没有" | 否则第二轮就成老模板，首轮告警被静默/冷却后就永远静默 |
| 存储 | SQLite + WAL，时间一律 UTC | 单机足够；原始 `run.json` 同时落盘，DB 坏了也不丢证据 |
| 报告目录 | 按**本地**日期 | 存储用 UTC，但人是按本地日期翻报告的 |

---

## 失败与降级

监控系统最大的敌人不是采不到数据，而是**静默失败** —— 程序正常退出、你以为"今天没异常"、实际一周没采到数据。

| 故障 | 表面现象 | 防护 | 是否实测 |
|---|---|---|---|
| 脚本崩溃 | 无 run 记录 | **心跳任务**用独立机制检查"预期 run 是否缺失" | ✅ |
| 采到 0 条 | "一切正常" | **0 条 = 失败**，显式判空并告警 | ✅ |
| AI 超时/报错/路径错 | 无 verdict | **降级**为规则层判定，结论标注"未经 AI 分析"，告警照发 | ✅ |
| AI 超预算 | 无 verdict | `max_calls_per_day` 闸门 → 转降级模式 | 代码就绪 |
| AI 幻觉乱报 | 告警轰炸 | AI 只产 verdict；脚本裁决；`confidence` 阈值 | 设计保证 |
| AI 漏报 | 无声 | 规则层设**兜底硬阈值**，不依赖 AI 也能触发 | ✅ |
| 告警通道挂 | 无人知道 | 落本地日志 + 发送失败不计冷却 → 下一轮重试 | ✅ |
| 计划任务没触发 | 全静默 | 心跳用**不同机制**实现 | ✅ |
| DB 写失败 | 数据丢失 | WAL + 原始 `run.json` 落盘兜底 | 设计保证 |
| 报错但看不懂 | 排查成本高 | `diagnose()` 把 dsh 原始报错翻译成"人话 + 怎么办"，并打到 stdout | ✅ |

---

## AI 判断层实测：它抓到了我自己写的 bug

这是最值得看的一段。第一次真实运行 AI 判断层时，它给出的证据里包含这样一条：

> `baseline.samples=1` 却记 `consecutive_failures=5`，而 `failures_24h=1`——
> 24 小时内仅 1 次失败却存在连续 5 次失败，时间跨度无法判断。
> **该不一致会削弱规则层触发条件的可信度。**

它是对的。`consecutive_failures` 当初**没有时间窗口**：昨天 17:05 的失败被算进了今天的"连续失败"，
而 `failures_24h` 有时间窗口 —— 两个指标自相矛盾。修复后：

- `failure_streak()` 限定 24 小时窗口，两个指标重新自洽；
- 额外输出 `streak_span_minutes`，因为"连续 5 次"可能是 5 分钟也可能是 5 天；
- 提示词里补齐了每个字段的**精确语义**，并明确要求"字段矛盾时先确认是不是时间窗口不同"。

修好之后，AI 的结论变成了：
> `consecutive_failures=2, streak_span_minutes=1.5, failures_24h=2` —— **两个窗口在本轮互相自洽**

**这件事本身就是这套架构的价值证明**：一个只会打印指标的系统不会告诉你指标之间矛盾。

### AI 归因的另一次实测（回归流水线）

对 3 个注入的回归，AI 的结论是：

> **3 个接口出现独立回归：2 处契约变更 + 1 处 404 语义缺失**
> ……同目录下的 `/api/users/9999` 同样场景返回了正确 404，
> 说明这**不是全局约定变更**，而是该接口自身缺失未找到分支。

最后那句交叉推理没有写在提示词里 —— 它是从"4 个通过的用例里包含 `user_not_found_status`"
自己推出来的。这正是"机器数数 + AI 判断"分工想要的效果。

### 日志分析流水线：又抓到两个（第 3 周）

**第一个**：AI 指出 `consecutive_failures` 与 `failures_24h` 用不同时间窗口（上一节的 bug），
这次它指向了基线设计本身：

> "39.3 倍'突增'更可能是上一窗口 220 次异常偏低造成的对比假象"
> "`history_avg`/`history_median` 均为 379 且 `history_windows=5`，与'历史从未出现'矛盾"

两条都指向同一个根因：**我把"故障期"的窗口写进了基线**。于是规则层和 AI 看到互相矛盾的信号，
而更严重的是——第 2 轮之后故障被"正常化"，`status=ok`，悄无声息。

修法两条，都是原则性的：

1. **基线只能由正常期构成**：`log_window.anomalous` 标记被判定为异常的窗口，排除在基线之外；
   参照值优先取"同文件的前一窗口"（固定不变的"之前"），只有它没有该模板时才退到历史**中位数**。
2. **"新"要用时间判据**：看 `first_seen` 是否在宽限期内（默认 3 天），
   而不是"历史窗口里有没有记录"。

修完之后的实测对比：

| 轮次 | 修复前 | 修复后 |
|---|---|---|
| 第 1 轮 | 检出 → 告警 | 检出 → 告警发出 |
| 第 2、3 轮 | `status=ok` **静默** | `severity=alert` **仍检出**，由**冷却**抑制重复 |

区别在于：持续故障由冷却机制管住噪声，而不是被误判成"新常态"。

---

## 目录结构

```
ops-pipeline/
├─ DESIGN.md              设计方案（含"实现与设计的偏差"附录）
├─ config/
│  ├─ config.toml         唯一需要你改的文件
│  ├─ dsh-headless.patch.yml  复用桌面账号登录；凭据仍从真实 ~/.dsh 读取
│  ├─ config.smoke.toml          冒烟测试：只打本地靶子、不走 AI
│  ├─ config.ai-test.toml        AI 判断层验证
│  └─ config.ai-degraded.toml    降级路径验证（故意写错 ai.cmd）
│     secrets.local.toml         本地密钥（凭据文件，已在 .gitignore 中；由 set-webhook.cmd 生成）
├─ ops/
│  ├─ __main__.py         入口：python -m ops --task X --once [--override ...]
│  ├─ core/
│  │  ├─ config.py        配置与凭据（凭据只从环境变量读）+ --override
│  │  ├─ http.py          探测与通用 JSON 请求（超时/重试/限速）
│  │  ├─ checks.py        接口契约断言 DSL（纯函数，独立单测）
│  │  ├─ logparse.py      日志模板提取 Drain 算法（纯 Python，独立单测）
│  │  ├─ store.py         SQLite(WAL) + schema + 列迁移
│  │  ├─ judge.py         规则判定 + 决策（纯函数，好测）
│  │  ├─ pipeline.py      收尾逻辑：判定→报告→裁决→告警→落库（所有流水线共用）
│  │  ├─ notify.py        企业微信 / 邮件 / dry-run
│  │  ├─ report.py        确定性报告渲染
│  │  └─ aibridge.py      调 dsh headless、DSH_HOME 隔离、解析契约、超时降级、错误翻译
│  ├─ tasks/
│  │  ├─ api_watch.py     接口可用性巡检
│  │  ├─ regression.py    接口回归 + 归因 + 晨报
│  │  ├─ log_scan.py      日志模板聚类 + 双窗口比对 + 归因 + 简报
│  │  ├─ daily_report.py  日报：汇总运行/告警/成本（固定会话）
│  │  └─ heartbeat.py     心跳
│  └─ prompts/            判断层提示词（与代码同管）
├─ targets/
│  ├─ service.py          靶子服务（v1 正常 / v2 注入 3 类回归）
│  └─ cases.py            7 条声明式回归用例
├─ tools/
│  ├─ fault_drill.py      接口故障靶子
│  ├─ loggen.py           合成日志生成器（可注入新错误模板 / 量级暴涨）
│  └─ notify_test.py      告警通道测试（配好 webhook 后先点一炮）
├─ scripts/               计划任务注册、告警通道配置、启动包装
└─ tests/                 151 个单元测试
```

---

## 告警邮件长什么样

**只讲人话** —— 这条是收件人反馈后改的：

```
🔴 严重　日志已停写超过24小时，服务很可能已经停了

这个应用已经一天多没有产生任何新日志，最后一条日志停在约24小时前，
本轮窗口里一条记录都没有。在停写之前的那个窗口，数据库连接失败和缓存
因内存不足清理失败的报错明显增多。现在看不到任何新日志，所以"没有报错"
并不等于正常。

**这意味着**：服务很可能已经停止运行，或者日志采集/轮转出了问题，
这段时间内的故障完全不可见，用户请求可能已经在失败。

**建议**：请先确认这个应用的进程是否还在运行、日志文件是否被轮转或采集
路径是否失效，然后再检查它的数据库连接和缓存内存情况。

———
日志源 0/1 正常，失败 1
完整报告：D:\AITool\ops-pipeline\data\reports\...\log_scan-....md
```

判断层通过 `plain_summary` / `impact` / `suggested_action` 产出这三块；
技术细节（`evidence`）**只进报告文件，不进邮件**。改之前是把 evidence 原样贴进邮件，
结果是满屏 `samples:` / `baseline.worldtime:` / `modal_body_hash`，收件人看到的是一堵墙。

判断层不可用时，规则层也会把异常类型翻译成人话（`judge.ANOMALY_PLAIN`）——
**降级 ≠ 难读**。有测试专门守着这条：`test_does_not_leak_machine_fields` 断言
邮件里不许出现 `samples:` / `baseline.` / `p95` / `status_code`。

## 验证状态

**已实测：**

- **225 个单元测试**全绿：分位数、模态、异常判定、冷启动、静默时段（含跨午夜）、
  决策抑制、规则兜底、存储幂等、冷却、dry-run 语义、连续失败时间窗口、断言 DSL 双侧覆盖、
  日志模板提取、窗口切分、静默失败防护、基线污染防护、`--override` 路径校验、
  企业微信 `errcode` 校验、密钥文件与 BOM 容错、控制台编码健壮性、
  事件流解析（sessionId / token）、日报汇总、恢复通知、**告警邮件不泄露机器字段**
- **恢复通知已实测**：制造异常 → 收到告警；换成正常输入 → **收到「已恢复」邮件**，
  且数据库里那条告警被标记 `resolved_at`，报告附注里也留了痕
- **日报已实测**：`daily_report` 汇总出 39 次运行 / 各任务状态分布 / token 用量，
  AI 用**固定会话**（`session-ded797c9-…`）写出的日报抓到了两个真问题 ——
  未恢复告警、以及"token 全为 0，可能是用量采集未接入"（当时确实是历史数据没有该字段）
- **token 用量已接入**：从 `dsh --json` 事件流解析，实测一次日报
  `in=9413 / out=4645 / cache_read=73344`（`totalTokens` 是累计值，不参与求和）
- **端到端冒烟**：健康轮 → `ok`；注入 500 → 前 2 轮仅记录 → **第 3 轮触发告警** →
  **第 4 轮被 `cooldown(0m<60m)` 抑制**；run/sample/alert/verdict 四张表逐条核对通过
- **AI 判断层真实跑通**：`ai=ok`，产出符合契约的 `verdict.json` 与报告；
  复用桌面账号登录，**无需 API Key**
- **AI 判断层可在受限沙箱内跑通**：子进程 `DSH_HOME` 已重定向到项目内 `data/dsh-home`，
  继承来的 `DSH_*` 一律剔除 → **零提权**（附录 C）
- **降级路径**：把 `ai.cmd` 指向不存在的路径 → `ai=failed` + 明确的修复指引，
  **告警照发**并标注"未经 AI 分析"
- **回归流水线**：v1 全绿 7/7；v2 抓到全部 3 个注入回归（字段改名 / 类型变化 / 状态码语义），
  AI 正确判定为"3 个独立回归"而非单点故障
- **日志分析流水线**：干净日志 `ok` 不告警；注入日志抓到 **379 次新错误模板** +
  **45 倍量级突增**（与生成器给出的 ground truth 完全一致）；
  持续故障**不会**被正常化（第 2、3 轮仍检出，由冷却抑制重复）
- **告警通道链路已用真实网络验证**：写入一个格式合法但 key 无效的 webhook 后调用，
  企业微信返回 `errcode=93000 invalid webhook url`，程序**正确判为 failed**
  （而不是被 HTTP 200 骗成"发送成功"）；密钥文件读取路径与打码显示也已验证
- **心跳**：列出一个"从未运行过"的任务，正确检出并告警，退出码 1
- **计划任务脚本**：`-DryRun` 预览通过（默认 3 个任务；`-IncludeLogScan` 为 4 个）；
  执行策略绕过与 UTF-8 BOM 问题已解决
- **计划任务已真实注册并跑通**：`OpsPipeline-ApiWatch` / `-Regression` / `-Heartbeat` 三个任务
  State=Ready；手动 `Start-ScheduledTask` 后 **`LastTaskResult = 0`**，
  `data/logs/launcher.log` 里能看到计划任务真实跑出的流水线输出
  （即"计划任务 → run_once.cmd → python -m ops"整条链已验证）

**尚未验证（诚实标注）：**

- **企业微信真实投递**：还没有用真实 webhook 成功投递过（那需要你自己的群机器人地址）。
  当前状态是 dry-run，消息落 `data/logs/notify.log`。
  链路本身已验证：用无效 key 调用会**正确报失败**而不是假报成功。
  你运行 `scripts\set-webhook.cmd` 后即可生效，工具会当场发一条测试消息确认
- 外部公开 API 目标的长时间稳定性观察（任务刚注册，还没有跑满一个周期）
- 日志分析尚未在**真实业务日志**上跑过（目前用的是合成日志 + 注入异常），
  因此 `log_scan` 也还没注册成计划任务

---

## 已知限制

- **巡检会话用新会话**：`api_watch` 每小时独立判断，不累积记忆。
  固定会话（有记忆）留给需要跨天对照的日报类任务 —— 这是刻意的，不是遗漏。
- **单机设计**：没有并发抓取、没有分布式，个人规模不需要。
- **公开 API 要礼貌**：默认 `concurrency` 顺序执行 + `rate_limit_ms`，只监控你有权调用的接口。
- **日志模板会折叠变量**：`status=200` 与 `status=500` 归进同一个模板（数字被泛化）。
  严重性靠**日志级别**判断，不靠模板里的状态码 —— 这是模板聚类的固有取舍，不是缺陷。
- **两个窗口需要 2×window_hours 的日志**：日志轮转太快（例如只留 12 小时）会让基线窗口为空，
  此时会退到历史基线或标记冷启动。

---

## 扩展一条新流水线

1. `ops/tasks/` 加一个模块，导出 `run(cfg, *, dry_run, no_ai) -> int`
2. `ops/__main__.py` 的 `TASKS` 字典里注册
3. `ops/prompts/` 加提示词 —— 关键是交代清楚**哪些是事实、哪些要你判断**
4. `config/config.toml` 加配置段；`[schedule.heartbeat_expect]` 加一条期望间隔
5. `scripts/register_tasks.ps1` 加一个触发器

`core/` 里的能力（存储、判定、告警、报告、AI 桥、收尾流水线）直接复用，不用重写。
`api_watch`、`regression`、`log_scan` 是三个可参考的完整例子 ——
它们分别代表"探测外部依赖"、"跑自己的用例"、"分析非结构化数据"三类形态。

---

## 重置

```powershell
Remove-Item -Recurse -Force data\ops.db*, data\reports, data\runs, data\state
```

`data/` 已在 `.gitignore` 中；凭据只走环境变量，仓库里只有 `.env.example`。
