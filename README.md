# 离线账号交接的例外账 · 本地复核工具

仅使用 **Windows 原生 Python 3.12 标准库**（无第三方包、无网络）。输入全部是
确定性合成数据。工具把策略实现成一个**仅追加、全序、事件溯源账本**和一个纯裁决
内核，并用第二套独立编写的 oracle 对拍；小输入做**全排列 + 窗口端点 + 连续滴答**
穷举，大输入做“**只追加一条吊销**”的差分复核；最终产出 **两张可独立核验的最小见证**。

---

## 1. 一分钟运行

在仓库根目录（本文件所在目录）打开 PowerShell / CMD / Git Bash：

```bat
python handoff_review.py all
```

或双击 / 执行：

```bat
run_review.bat
```

无参数运行 `python handoff_review.py` 等价于 `all`。成功时最后一行是
`总体结果： ALL PASS`，**退出码 0**；任何断言/核验失败则退出码 1。

只核验已经生成的两张见证（独立路径，不重新生成）：

```bat
python verify_witness.py
python verify_witness.py witnesses\witness_1_handoff_effective.json
python verify_witness.py witnesses\witness_2_earliest_denial.json
```

也可用主 CLI：`python handoff_review.py verify <见证.json ...>`。

其它子命令：`small`（只跑小输入穷举并打印 JSON）、`large`（只跑大输入差分报告）。

---

## 2. 题面要求 ↔ 实现位置

| 题面要求 | 实现 / 证据 |
|---|---|
| 同一受托人不同 **职 / 班次** 权限不互相继承 | 身份被建模为精确作用域 `assignment = org + team + post + shift`；权限只发给具体 `assignment_id`。`decide()` 在精确身份无来源时，即使在同一人/同组织的另一身份上发现来源，也返回 `scope_permission_not_inherited`（`handoff_review.py` 的 `_foreign_scope_source`）。小输入对“仅班次不同 / 仅职不同”的兄弟身份在**所有排列、所有滴答**下断言永不允许；大输入每个交接都带 `NIGHT-DENY` / `POST-DENY` 负向探针并统计继承违规数=0。 |
| **批准时间 / 执行时间 / 读取水位分开** | 三个独立字段：`approve.approved_at=10`、`execute.executed_at=15`、报告的 `read_watermark`（大输入目标为 500）。裁决依次检查“是否已批准/已执行（账本可见性）”与 `access_at >= approved_at`、`access_at >= executed_at`（业务时间）；历史报告用 `event_time(e) <= read_watermark` 取前缀，与当前状态矩阵完全分开。 |
| 例外只在 **明确批准过的接班窗口** 生效 | 窗口 `[window_start, window_end)` 由 `approve` 事件显式给出（小输入 `[20,30)`）。默认半开：20 允许、29 是最后允许滴答、30 拒绝。工具**独立枚举** `start-1/start/start+1` 与 `end-1/end/end+1`，并额外跑闭区间 `[start,end]` 模式锁定端点语义；连续扫描 `t=0..30`，允许集合必须恰为 `20..29`。 |
| 批准后仍可被 **值班主任即时吊销** | `revoke_exception(..., by_role="duty_director")` 置位后，自该账本位置起后续裁决立即得到 `exception_revoked_by_duty_director`，无传播延迟。非主任角色的“吊销”不生效。同一滴答竞争由账本全序裁决（吊销事件排在请求前则拒，请求排在吊销前则该次仍允许）。**吊销只杀例外，不影响队内权限。** |
| 输出 **两张可独立核验的最小见证** | `witnesses/witness_1_handoff_effective.{json,md}`（交接生效）与 `witness_2_earliest_denial.{json,md}`（最早拒绝时点，含即时吊销边界）。JSON 自包含原始 `entities`+`trace` 与 `core_digest`。核验方只重放原始事件、重跑独立 oracle 与最小性枚举，**不信任文件里的 verdict 布尔**。 |
| 小输入：**所有短交错** 独立枚举 | `enumerate_interleavings()` 对 6 事件族做全部 `6!=720` 排列、5 事件作用域族做全部 `5!=120` 排列（不抽样），每个 access 都做内核↔oracle 原因码逐项对拍，并检查来源作用域、不继承、吊销即时性等不变量。 |
| 小输入：**窗口端点** 独立枚举 | `enumerate_endpoints()` 产生 `2 模式 ×2 吊销情形 ×4 作用域 ×15 滴答 ×2 能力 = 480` 个探测并对拍；另含显式左/右端点断言。 |
| 大输入：追加 **一条吊销** 对历史报告的影响 | `run_large_review()` 生成 24 组织 / 4322 作业身份 / 约数千探针 / 数十份水位报告；先固化每份历史报告的规范化哈希，再**只追加一条** `EX-TARGET` 吊销，重算：所有固定水位报告逐字节不变（新吊销时间 20000 高于全部水位，被水位排除），目标在水位 500 的历史 ALLOW 仍保留。 |
| 大输入：对 **其他组织** 访问的影响 | 同一追加前后计算全矩阵并逐条 diff：**恰好一个**裁决变化（`TARGET-EXC-NOW`：`allowed_exception → exception_revoked_by_duty_director`）；所有 `org != ORG-000` 的裁决逐条相等，本组织内无关身份、队内权限、兄弟班次负向控制也全部不变。 |

---

## 3. 两张见证怎么读

### 见证一 · 交接生效（W1）

最小轨迹只有三个操作事件：`approve@10(window[20,30)) → execute@15 → access ticket.handle@16 (ASG-DAY-IC)`，
最终 `allowed_team_permission`。最小性由两部分支撑：

- 穷举 `[approve, execute, access]` 的全部前缀与排列，产生 ALLOW 的最少事件数 = 3；
- 必要性变异：移除批准 / 移除执行 / 把访问改到 `executed_at-1` / 换成夜班身份，
  四种变异都必须翻转为拒绝（核验器还会独立重放其中两个变异）。

### 见证二 · 最早拒绝时点（W2）

“最早”有两种严谨口径，见证同时给出并分别做了最小性认证，避免把任意网格下界误当边界：

- **口径 A — 声明有限宇宙中字面上最早的拒绝**：宇宙大小 `2(吊销情形)×4(作用域)×2(能力)×15(滴答)=240`。
  按 `(滴答, 吊销情形, 作用域优先级, 能力优先级)` 取最小 DENY，代表轨迹是
  `approve@10 → execute@15 → 例外访问@9`，原因 `before_approval_time`（访问早于批准时间）。
  比该排序键更早的候选数/拒绝数均为 0。
- **口径 B — 一张本可生效的例外的最早 allow→deny 边界（即时吊销）**：成对括号轨迹
  `approve → execute → pre@r → revoke@r(主任) → post@r`。枚举候选 `r∈{19,20,21,24,25,29,30}`，
  最早满足“pre ALLOW、post DENY(revoked)”的是 **r=20**（窗口起点，例外最早可行使的滴答；
  主任可在该滴答以账本顺序先完成吊销）。更早的此类边界数=0。
- 对照的被动端点边界：窗口关闭 **t=30**（t=29 ALLOW → t=30 `exception_outside_approved_window`），
  晚于吊销边界 20。连续滴答扫描证明无吊销时允许集合恰为 20..29。

> 说明：若把时间轴向过去无限延伸，“绝对最早的一次拒绝”没有有限答案（总能更早发请求），
> 因此工具只在**显式声明的有限宇宙/连续区间**内主张“字面最早”，并把非任意的“最早边界”
> 定义为 allow→deny 的翻转点。这是形式核验中“最早反例时点”的标准有限化做法。

---

## 4. 独立核验为什么可信

`verify_witness_file()`（`verify_witness.py` 是它的薄入口）：

1. 读取见证 JSON，重算 `core_digest` 做完整性比对；
2. **只取文件里的原始 `entities` 与轨迹事件**，用纯内核重新折叠/重放，得到最终裁决；
3. 用独立写法的 `oracle_decide()`（在原始事件列表上扫描，不复用 `decide/fold`）再次判定并比对原因码；
4. 见证二还会**重新运行整个 `certify_earliest()` 穷举**，独立确认宇宙大小、字面最早滴答/原因、
   连续窗口集合、吊销边界与其最小性，而不是读取文件中写好的“通过”布尔。

退出码 0 表示所有核验项通过。

---

## 5. 产物布局

```
handoff_review.py          # 唯一主程序：模型/内核/oracle/枚举/大输入/见证/CLI
verify_witness.py          # 两张见证的独立核验入口（薄封装，仅标准库）
run_review.bat             # Windows 一键运行
witnesses/                 # 两张见证（运行 all 后生成；JSON + Markdown）
artifacts/review/          # 机器可读的枚举/差分/核验报告与 run_summary.json
```

`artifacts/` 在 `.gitignore` 中；`witnesses/` 不被忽略，便于把两张见证作为交付物留存。

## 6. 设计约定（可被验证地锁定）

- 账本全序、仅追加；请求只能看见它之前的事件前缀。
- 窗口默认半开 `[start,end)`；同时枚举闭区间模式。
- 同一滴答的批准/执行/吊销/访问竞争由账本顺序裁决。
- “执行早于批准”的执行不获授权，且不被事后到达的批准追认（失败闭合）。
- 读取水位选择 `event_time <= watermark` 的事件；追加未来吊销不会改写固定水位的历史报告。
- 所有随机大输入使用固定种子 `20261126`，可复现。
