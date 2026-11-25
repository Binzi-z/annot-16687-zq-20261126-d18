# 见证二 · 最早拒绝时点（W2-EARLIEST-DENIAL）

> 引导版叙述；`python handoff_review.py all` 会重新生成同名 `.md/.json` 并钉住摘要。
> 机器核验以 JSON + `verify_witness.py` 为准（核验器会**重新枚举整个有限宇宙**证明“最早”）。

“最早”用两种严谨口径给出，避免把任意时间下界误当边界。

## 口径 A · 声明有限宇宙中字面上最早的拒绝：t = 9

- 宇宙 = 2(吊销情形) × 4(作用域) × 2(能力) × 15(边界滴答) = **240** 个独立枚举探测。
- 最小 DENY 键为 `(t=9, 未吊销, self, exc)`：在 `approved_at=10` 之前行使例外，
  裁决 **`before_approval_time`**。比该键更早的候选数 = 0、拒绝数 = 0。

最小轨迹：`approve@10(window[20,30)) → execute@15 → 例外访问@9` → **DENY `before_approval_time`**。

## 口径 B · 一张本可生效例外的最早 allow→deny 边界：t = 20（即时吊销）

成对括号轨迹（同一滴答 t=20，先后由账本全序决定）：

| # | 事件 | 结果 |
|---:|---|---|
| 1 | `approve@10 window[20,30)` | — |
| 2 | `execute@15` | — |
| 3 | `access refund.override @20`（吊销前） | **ALLOW `allowed_exception`**（窗口起点含） |
| 4 | `revoke EX-1 @20 by director.zhou (duty_director)` | 例外即时失效 |
| 5 | `access refund.override @20`（吊销后） | **DENY `exception_revoked_by_duty_director`** |

候选 `r ∈ {19,20,21,24,25,29,30}` 全部独立枚举：最早满足“pre ALLOW、post revoked-DENY”的是
**r=20**；更早的此类边界数 = **0**。r=19 时 pre 尚在窗口外，r=30 时 pre 已到窗口右端，
都不构成“有效访问被吊销打断”的边界。

## 端点与连续扫描（独立枚举）

- 半开窗口 `[20,30)`：t=19 `outside_window`、t=20 ALLOW、t=29 ALLOW、t=30 `outside_window`、t=31 `outside_window`。
- 连续扫描 `t=0..30`：允许集合**恰好** `[20..29]`，且内核与独立 oracle 原因码逐项一致。
- 被动窗口关闭边界为 **t=30**（t=29 ALLOW → t=30 `exception_outside_approved_window`），晚于吊销边界 20。

## 不继承（同一受托人，不同职/班次）

无论怎样交错、是否吊销、取哪个滴答：夜班身份 `ASG-NIGHT-IC` 或操作员身份 `ASG-DAY-OPERATOR`
对交接专属能力的请求都得到 **`scope_permission_not_inherited`**；权限不跨职/班次继承。

## 独立核验

```bat
python verify_witness.py witnesses\witness_2_earliest_denial.json
```

核验器会重放上面三条轨迹、用独立 oracle 对拍，并重新运行 `certify_earliest()` 全宇宙枚举，
独立确认 240 的宇宙大小、t=9 的字面最早、`[20..29]` 连续允许集与 t=20 的吊销边界最小性。
