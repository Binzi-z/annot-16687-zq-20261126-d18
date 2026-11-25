# 见证一 · 交接生效（W1-HANDOFF-EFFECTIVE）

> 这是随仓库提供的**引导版叙述**；运行 `python handoff_review.py all` 会用工具重新生成
> 同名 `.md/.json` 并钉住 `core_digest`。机器核验以 JSON + `verify_witness.py` 为准。

**结论：`allowed_team_permission`** —— 交接经 `manager.wang` 批准、由 `handoff-bot` 执行后，
受托人 `trustee.renee` 以**精确的日班现场指挥身份 `ASG-DAY-IC`** 在 t=16 行使 `ticket.handle` 被允许。

- 批准时间 `approved_at = 10`，批准窗口 `[20,30)`（本见证行使的是队内权限，不受窗口限制）。
- 执行时间 `executed_at = 15`；访问 t=16 ≥ 执行时间。三个时间（批准/执行/读取水位）互不替代。
- 权限来源作用域精确等于请求作用域 `ASG-DAY-IC`，**不涉及任何职/班次继承**。

## 最小轨迹（原始事件，可重放）

| # | 事件 | 关键字段 |
|---:|---|---|
| 1 | `approve` | `approved_at=10`, `window=[20,30)` |
| 2 | `execute` | `executed_at=15`（批准已在前缀中，执行获授权） |
| 3 | `access` | `at=16`, as `ASG-DAY-IC`, cap=`ticket.handle` → **ALLOW** |

## 最小性 / 必要性

- 穷举 `[approve, execute, access]` 的全部前缀与排列：产生 ALLOW 的最少事件数 = **3**。
- 移除批准 → `ticket_not_approved`；移除执行 → `handoff_not_executed`。
- 访问改到 `executed_at-1=14` → `before_execution_time`。
- 改成夜班身份 `ASG-NIGHT-IC` → `scope_permission_not_inherited`（不继承）。

## 独立核验

```bat
python verify_witness.py witnesses\witness_1_handoff_effective.json
```

核验器重放 `entities + trace`、用独立 oracle 对拍，并独立重放必要性变异；不信任 JSON 里的结论字段。
