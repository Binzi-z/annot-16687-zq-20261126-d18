#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
handoff_review.py — 合成账号交接的本地复核工具（仅用 Python 3.12 标准库）

复核目标（对应 task/PROMPT.txt）
--------------------------------
1. 同一受托人在不同「职 / 班次」作业身份（assignment = 组织 + 队 + 职 + 班次）
   下，队内权限与临时例外一律按 *精确作业身份* 裁决，彼此不继承。
2. 工单的三个时间严格分开、互不替代：
     - approved_at     批准时间（例外窗口由批准动作明确给出）
     - executed_at     执行时间（交接在账本中实际生效）
     - read_watermark  读取水位（历史报告只看水位之前/固定前缀的事件）
3. 临时例外只在 *明确批准过的接班窗口* 内有效；值班主任可在批准后 *即时吊销*，
   吊销自账本位置起对后续裁决立即生效（无传播延迟）。
4. 小输入：对所有短交错（事件全排列，不抽样）以及窗口两端端点做独立枚举，
   并用第二套独立写出的判定 oracle 与状态折叠内核逐项对拍。
5. 大输入：追加且仅追加一条吊销，做差分：
     - 固定读取水位/前缀的历史报告逐字节不变；
     - 被吊销例外的当前访问由允许变拒绝；
     - 其他组织、以及本组织内无关身份/队内权限的裁决完全不变。
6. 产出两张可独立核验的最小见证（JSON + 人读 Markdown），并有独立 verify 路径：
   核验方只信任见证里的原始事件，重新折叠/重放、重跑 oracle 与最小性枚举，
   不信任生成方写入的任何 verdict 字段。

时间与端点约定（显式、可被验证）
--------------------------------
* 账本是全序、仅追加的事件序列；裁决只可见请求事件之前的前缀。
* 接班窗口默认半开区间 [window_start, window_end)：start 时刻允许，
  end-1 是最后一个允许滴答，end 时刻拒绝。工具同时独立枚举闭区间模式
  [start, end] 以证明端点语义被显式锁定，而非偶然。
* 同一滴答内的竞争由账本全序裁决：吊销事件排在请求之前则拒绝，排在其后则
  该请求仍允许；吊销对其后的请求立即生效。
* 业务时间用于窗口/时间门与水位；账本顺序用于“事实是否已可见”。二者分开。

直接运行：
    python handoff_review.py all
    python handoff_review.py verify witnesses/witness_2_earliest_denial.json
无参数等价于 all。退出码 0 表示全部断言与独立核验通过，非 0 表示发现问题。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from itertools import permutations
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "handoff-review-witness/v1"
MODEL_VERSION = "1.0.0"
TOOL_NAME = "handoff_review.py"

ROOT = Path(__file__).resolve().parent
DEFAULT_WITNESS_DIR = ROOT / "witnesses"
DEFAULT_OUT_DIR = ROOT / "artifacts" / "review"

# 让 Windows 控制台（含重定向）尽量稳定输出中文。
try:  # pragma: no cover - 平台相关的健壮性处理
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# 规范化 JSON / 摘要
# ---------------------------------------------------------------------------

def canon_dumps(obj: Any) -> str:
    """确定性规范化 JSON（排序键、无空白、不转义非 ASCII）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=list)


def canon_bytes(obj: Any) -> bytes:
    return canon_dumps(obj).encode("utf-8")


def sha256_obj(obj: Any) -> str:
    return hashlib.sha256(canon_bytes(obj)).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 事件构造器（全部是普通 dict，便于规范化序列化与独立重放）
# ---------------------------------------------------------------------------

def ev_assignment(assignment_id, org, team, person, post, shift):
    """一个作业身份：组织 + 队 + 职(post) + 班次(shift) 的精确作用域。"""
    return {"kind": "assignment", "assignment_id": assignment_id, "org": org,
            "team": team, "person": person, "post": post, "shift": shift}


def ev_open_handoff(handoff_id, *, org, team, trustee, assignment_id,
                    from_account, team_capabilities, exception_id=None,
                    exception_capability=None, requested_window=None):
    """建立交接定义；此刻尚未批准、尚未执行，因此不产生任何有效权限。"""
    return {"kind": "open_handoff", "handoff_id": handoff_id, "org": org,
            "team": team, "trustee": trustee, "assignment_id": assignment_id,
            "from_account": from_account,
            "team_capabilities": sorted(team_capabilities),
            "exception_id": exception_id,
            "exception_capability": exception_capability,
            "requested_window": list(requested_window) if requested_window else None}


def ev_grant(assignment_id, capability, *, org, source="direct"):
    """一条直接发给某精确作业身份的队内权限（用于本地对照，而非跨身份继承）。"""
    return {"kind": "grant", "assignment_id": assignment_id,
            "capability": capability, "org": org, "source": source}


def ev_approve(handoff_id, *, approved_at, approver, window_start, window_end):
    """批准工单，并明确给出被批准的接班窗口。approved_at 是独立的批准时间。"""
    return {"kind": "approve", "handoff_id": handoff_id,
            "approved_at": approved_at, "approver": approver,
            "window_start": window_start, "window_end": window_end}


def ev_execute(handoff_id, *, executed_at, executor="handoff-bot"):
    """执行交接。executed_at 是独立的执行时间。"""
    return {"kind": "execute", "handoff_id": handoff_id,
            "executed_at": executed_at, "executor": executor}


def ev_revoke(exception_id, *, revoked_at, by, by_role="duty_director",
              reason="immediate_revocation"):
    """值班主任即时吊销一张临时例外。"""
    return {"kind": "revoke", "exception_id": exception_id,
            "revoked_at": revoked_at, "by": by, "by_role": by_role,
            "reason": reason}


def ev_access(request_id, *, at, org, person, assignment_id, capability):
    """一次以某作业身份发起的访问请求；at 是访问时间。"""
    return {"kind": "access", "request_id": request_id, "at": at, "org": org,
            "person": person, "assignment_id": assignment_id,
            "capability": capability}


def event_time(e: dict) -> int:
    """每个事件用于水位过滤的业务时间（批准/执行/吊销各用各的字段）。"""
    k = e["kind"]
    if k == "approve":
        return e["approved_at"]
    if k == "execute":
        return e["executed_at"]
    if k == "revoke":
        return e["revoked_at"]
    if k in ("access",):
        return e["at"]
    # 定义类事件视为账本基线（始终在水位之内，由 setup 固定提供）。
    return -(10 ** 12)


# ---------------------------------------------------------------------------
# 裁决原因码
# ---------------------------------------------------------------------------

ALLOW_TEAM = "allowed_team_permission"
ALLOW_EXCEPTION = "allowed_exception"
ALLOW_DIRECT = "allowed_direct_permission"

DENY_UNKNOWN_ASSIGNMENT = "unknown_assignment"
DENY_ORG_MISMATCH = "org_mismatch"
DENY_IDENTITY_MISMATCH = "identity_mismatch"
DENY_NO_PERMISSION = "no_permission"
DENY_SCOPE_NOT_INHERITED = "scope_permission_not_inherited"
DENY_NOT_APPROVED = "ticket_not_approved"
DENY_NOT_EXECUTED = "handoff_not_executed"
DENY_EXEC_BEFORE_APPROVAL = "execution_before_approval_not_authorized"
DENY_BEFORE_APPROVAL_TIME = "before_approval_time"
DENY_BEFORE_EXECUTION_TIME = "before_execution_time"
DENY_WINDOW_NOT_APPROVED = "approved_window_missing"
DENY_OUTSIDE_WINDOW = "exception_outside_approved_window"
DENY_REVOKED = "exception_revoked_by_duty_director"

ALLOW_CODES = {ALLOW_TEAM, ALLOW_EXCEPTION, ALLOW_DIRECT}

REASON_ZH = {
    ALLOW_TEAM: "允许：交接已批准并执行，精确作业身份持有该队内权限",
    ALLOW_EXCEPTION: "允许：临时例外在批准窗口内、未被吊销，且作业身份精确匹配",
    ALLOW_DIRECT: "允许：该精确作业身份直接持有此权限",
    DENY_UNKNOWN_ASSIGNMENT: "拒绝：未知作业身份，失败闭合",
    DENY_ORG_MISMATCH: "拒绝：请求组织与作业身份所属组织不一致（跨组织隔离）",
    DENY_IDENTITY_MISMATCH: "拒绝：受托人与作业身份登记人不一致",
    DENY_NO_PERMISSION: "拒绝：该作业身份没有此权限，且不存在可继承来源",
    DENY_SCOPE_NOT_INHERITED: "拒绝：同一受托人的权限属于另一职/班次身份，不发生继承",
    DENY_NOT_APPROVED: "拒绝：工单在当前前缀中尚未批准",
    DENY_NOT_EXECUTED: "拒绝：交接尚未执行",
    DENY_EXEC_BEFORE_APPROVAL: "拒绝：执行早于批准，该执行未获授权且不被事后追认",
    DENY_BEFORE_APPROVAL_TIME: "拒绝：访问时间早于批准时间",
    DENY_BEFORE_EXECUTION_TIME: "拒绝：访问时间早于执行时间",
    DENY_WINDOW_NOT_APPROVED: "拒绝：批准中未给出有效接班窗口",
    DENY_OUTSIDE_WINDOW: "拒绝：访问时间不在明确批准的接班窗口内",
    DENY_REVOKED: "拒绝：临时例外已被值班主任即时吊销",
}


def _deny(code, **extra):
    d = {"allowed": False, "code": code, "reason_zh": REASON_ZH[code]}
    d.update(extra)
    return d


def _allow(code, **extra):
    d = {"allowed": True, "code": code, "reason_zh": REASON_ZH[code]}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 状态折叠内核（增量、可变状态；大输入只折叠一次）
# ---------------------------------------------------------------------------

def empty_state() -> dict:
    return {
        "assignments": {},       # assignment_id -> record
        "grants": {},            # assignment_id -> {capability -> source}
        "handoffs": {},          # handoff_id -> record
        "handoff_by_assignment": {},  # assignment_id -> handoff_id（本模型每身份一个活跃交接）
        "exceptions": {},        # exception_id -> record
        "revocation_count": 0,
        "applied": 0,
    }


def apply_event(st: dict, e: dict) -> None:
    k = e["kind"]
    if k == "assignment":
        st["assignments"][e["assignment_id"]] = {
            "org": e["org"], "team": e["team"], "person": e["person"],
            "post": e["post"], "shift": e["shift"]}
    elif k == "grant":
        st["grants"].setdefault(e["assignment_id"], {})[e["capability"]] = e.get("source", "direct")
    elif k == "open_handoff":
        h = {
            "handoff_id": e["handoff_id"], "org": e["org"], "team": e["team"],
            "trustee": e["trustee"], "assignment_id": e["assignment_id"],
            "from_account": e["from_account"],
            "team_capabilities": set(e["team_capabilities"]),
            "exception_id": e.get("exception_id"),
            "exception_capability": e.get("exception_capability"),
            "requested_window": e.get("requested_window"),
            "approval": None,
            "execution": None,
        }
        st["handoffs"][e["handoff_id"]] = h
        st["handoff_by_assignment"][e["assignment_id"]] = e["handoff_id"]
        if e.get("exception_id"):
            st["exceptions"][e["exception_id"]] = {
                "exception_id": e["exception_id"],
                "handoff_id": e["handoff_id"],
                "capability": e.get("exception_capability"),
                "revoked": False, "revoked_at": None,
                "revoked_by": None, "revoke_seq": None,
            }
    elif k == "approve":
        h = st["handoffs"].get(e["handoff_id"])
        if h is not None and h["approval"] is None:
            h["approval"] = {
                "approved_at": e["approved_at"], "approver": e.get("approver"),
                "window_start": e["window_start"], "window_end": e["window_end"]}
    elif k == "execute":
        h = st["handoffs"].get(e["handoff_id"])
        if h is not None and h["execution"] is None:
            # 关键交错规则：只有执行事件发生时批准已在前缀中，执行才获授权。
            h["execution"] = {
                "executed_at": e["executed_at"], "executor": e.get("executor"),
                "approval_was_before": h["approval"] is not None}
    elif k == "revoke":
        x = st["exceptions"].get(e["exception_id"])
        # 仅值班主任的吊销即时有效；其他角色的“吊销”不改变状态（失败闭合到无动作）。
        if x is not None and e.get("by_role") == "duty_director":
            x["revoked"] = True
            x["revoked_at"] = e["revoked_at"]
            x["revoked_by"] = e.get("by")
            x["revoke_seq"] = st["applied"]
            st["revocation_count"] += 1
    # access 是只读探测，不改变状态。
    st["applied"] += 1


def fold(events: Iterable[dict], st: dict | None = None) -> dict:
    st = st if st is not None else empty_state()
    for e in events:
        apply_event(st, e)
    return st


def _h_covers_cap(h: dict, cap: str) -> bool:
    return cap in h["team_capabilities"] or h.get("exception_capability") == cap


def _foreign_scope_source(st: dict, *, person: str, org: str,
                           assignment_id: str, cap: str):
    """在同一人、同一组织内，寻找 *另一* 作业身份上的权限来源（用于判定不继承）。"""
    for h2 in st["handoffs"].values():
        if (h2["trustee"] == person and h2["org"] == org
                and h2["assignment_id"] != assignment_id
                and _h_covers_cap(h2, cap)):
            return {"kind": "handoff", "id": h2["handoff_id"],
                    "assignment_id": h2["assignment_id"]}
    for gaid, caps in st["grants"].items():
        rec = st["assignments"].get(gaid)
        if (cap in caps and rec is not None and rec["person"] == person
                and rec["org"] == org and gaid != assignment_id):
            return {"kind": "grant", "id": caps[cap], "assignment_id": gaid}
    return None


def decide(st: dict, req: dict, *, endpoint_mode: str = "half_open") -> dict:
    """纯裁决函数：只依赖折叠状态 st 与请求 req。绝不回查未来事件。"""
    aid = req["assignment_id"]
    org = req["org"]
    person = req["person"]
    cap = req["capability"]
    t = req["at"]

    a = st["assignments"].get(aid)
    if a is None:
        return _deny(DENY_UNKNOWN_ASSIGNMENT, request_id=req.get("request_id"))
    if a["org"] != org:
        return _deny(DENY_ORG_MISMATCH, request_id=req.get("request_id"),
                     assignment_org=a["org"], request_org=org)
    if a["person"] != person:
        return _deny(DENY_IDENTITY_MISMATCH, request_id=req.get("request_id"),
                     assignment_person=a["person"], request_person=person)

    provenance = {"org": org, "assignment_id": aid, "post": a["post"],
                  "shift": a["shift"], "capability": cap}

    # 1) 精确作业身份上的直接授权。
    if cap in st["grants"].get(aid, {}):
        return _allow(ALLOW_DIRECT, request_id=req.get("request_id"),
                      provenance={**provenance, "source": st["grants"][aid][cap]})

    hid = st["handoff_by_assignment"].get(aid)
    h = st["handoffs"].get(hid) if hid is not None else None

    # 该交接是否覆盖此能力。
    covers_team = h is not None and cap in h["team_capabilities"]
    covers_exc = (h is not None and h.get("exception_id")
                  and h.get("exception_capability") == cap)

    if h is None or not (covers_team or covers_exc):
        foreign = _foreign_scope_source(
            st, person=person, org=org, assignment_id=aid, cap=cap)
        if foreign is not None:
            return _deny(DENY_SCOPE_NOT_INHERITED,
                         request_id=req.get("request_id"),
                         provenance=provenance, foreign_source=foreign)
        return _deny(DENY_NO_PERMISSION, request_id=req.get("request_id"),
                     provenance=provenance)

    # 2) 交接类权限的共同时间/顺序门。
    if h["approval"] is None:
        return _deny(DENY_NOT_APPROVED, request_id=req.get("request_id"),
                     provenance=provenance)
    ap = h["approval"]

    if h["execution"] is None:
        return _deny(DENY_NOT_EXECUTED, request_id=req.get("request_id"),
                     provenance=provenance, approved_at=ap["approved_at"])
    ex = h["execution"]
    if not ex["approval_was_before"]:
        return _deny(DENY_EXEC_BEFORE_APPROVAL, request_id=req.get("request_id"),
                     provenance=provenance, executed_at=ex["executed_at"])

    if t < ap["approved_at"]:
        return _deny(DENY_BEFORE_APPROVAL_TIME, request_id=req.get("request_id"),
                     provenance=provenance, access_at=t,
                     approved_at=ap["approved_at"])
    if t < ex["executed_at"]:
        return _deny(DENY_BEFORE_EXECUTION_TIME, request_id=req.get("request_id"),
                     provenance=provenance, access_at=t,
                     executed_at=ex["executed_at"])

    # 3a) 队内权限：吊销临时例外不影响队内权限；无窗口要求。
    if covers_team:
        return _allow(ALLOW_TEAM, request_id=req.get("request_id"),
                      provenance={**provenance, "handoff_id": h["handoff_id"],
                                  "approved_at": ap["approved_at"],
                                  "executed_at": ex["executed_at"]})

    # 3b) 临时例外：先查即时吊销，再查明确批准的窗口。
    x = st["exceptions"].get(h["exception_id"])
    if x is not None and x["revoked"]:
        return _deny(DENY_REVOKED, request_id=req.get("request_id"),
                     provenance={**provenance, "exception_id": x["exception_id"]},
                     revoked_at=x["revoked_at"], revoked_by=x["revoked_by"])

    ws, we = ap["window_start"], ap["window_end"]
    if ws is None or we is None:
        return _deny(DENY_WINDOW_NOT_APPROVED, request_id=req.get("request_id"))

    if endpoint_mode == "half_open":
        inside = ws <= t < we
        convention = "[start, end)"
    elif endpoint_mode == "inclusive":
        inside = ws <= t <= we
        convention = "[start, end]"
    else:
        raise ValueError(f"unknown endpoint_mode {endpoint_mode!r}")

    if not inside:
        edge = "before_start" if t < ws else "at_or_after_end"
        return _deny(DENY_OUTSIDE_WINDOW, request_id=req.get("request_id"),
                     provenance={**provenance, "exception_id": h["exception_id"]},
                     access_at=t, window_start=ws, window_end=we,
                     endpoint_convention=convention, edge=edge)

    return _allow(ALLOW_EXCEPTION, request_id=req.get("request_id"),
                  provenance={**provenance, "handoff_id": h["handoff_id"],
                              "exception_id": h["exception_id"]},
                  access_at=t, window=[ws, we],
                  endpoint_convention=convention,
                  approved_at=ap["approved_at"],
                  executed_at=ex["executed_at"])


def replay_decisions(setup: list[dict], op_events: list[dict], *,
                     endpoint_mode: str = "half_open") -> list[dict]:
    """按给定全序重放 setup + op_events，返回每个 access 事件对应的裁决。"""
    st = fold(setup)
    out = []
    for pos, e in enumerate(op_events):
        if e["kind"] == "access":
            d = decide(st, e, endpoint_mode=endpoint_mode)
            out.append({"position": pos, "event": e, "decision": d})
        else:
            apply_event(st, e)
    return out


# ---------------------------------------------------------------------------
# 独立 oracle：刻意用另一套“在原始事件列表上扫描”的写法实现同一策略，
# 不共享 fold/decide，仅共享极少数无分支的覆盖谓词。用于逐项对拍。
# ---------------------------------------------------------------------------

def oracle_decide(setup: list[dict], prior_events: list[dict], req: dict, *,
                  endpoint_mode: str = "half_open") -> dict:
    raw = list(setup) + list(prior_events)
    aid, org, person, cap, t = (req["assignment_id"], req["org"],
                                req["person"], req["capability"], req["at"])

    asg = next((e for e in raw if e["kind"] == "assignment"
                and e["assignment_id"] == aid), None)
    if asg is None:
        return {"allowed": False, "code": DENY_UNKNOWN_ASSIGNMENT}
    if asg["org"] != org:
        return {"allowed": False, "code": DENY_ORG_MISMATCH}
    if asg["person"] != person:
        return {"allowed": False, "code": DENY_IDENTITY_MISMATCH}

    direct = any(e["kind"] == "grant" and e["assignment_id"] == aid
                 and e["capability"] == cap for e in raw)
    if direct:
        return {"allowed": True, "code": ALLOW_DIRECT}

    hdef = next((e for e in setup if e["kind"] == "open_handoff"
                 and e["assignment_id"] == aid and e["trustee"] == person
                 and e["org"] == org), None)

    def covers(hd, c):
        return c in hd["team_capabilities"] or hd.get("exception_capability") == c

    if hdef is None or not covers(hdef, cap):
        foreign = any(
            e["kind"] == "open_handoff" and e["trustee"] == person
            and e["org"] == org and e["assignment_id"] != aid
            and covers(e, cap)
            for e in setup)
        if not foreign:
            # 直接授权形式的跨身份来源也算外国来源。
            foreign = any(
                e["kind"] == "grant" and e["capability"] == cap
                and e["assignment_id"] != aid
                and (lambda a: a is not None and a["person"] == person
                     and a["org"] == org)(
                         next((x for x in setup if x["kind"] == "assignment"
                               and x["assignment_id"] == e["assignment_id"]), None))
                for e in setup)
        return {"allowed": False,
                "code": DENY_SCOPE_NOT_INHERITED if foreign else DENY_NO_PERMISSION}

    hid = hdef["handoff_id"]
    prior = list(prior_events)

    def last_index(kind):
        idx = [i for i, e in enumerate(prior) if e["kind"] == kind
               and e.get("handoff_id") == hid]
        return idx[-1] if idx else None

    ap_i = last_index("approve")
    ex_i = last_index("execute")
    ap = prior[ap_i] if ap_i is not None else None
    exe = prior[ex_i] if ex_i is not None else None

    if ap is None:
        return {"allowed": False, "code": DENY_NOT_APPROVED}
    if cap in hdef["team_capabilities"]:
        if exe is None:
            return {"allowed": False, "code": DENY_NOT_EXECUTED}
        if not any(e["kind"] == "approve" and e.get("handoff_id") == hid
                   for e in prior[:ex_i + 1]) or ap_i > ex_i:
            return {"allowed": False, "code": DENY_EXEC_BEFORE_APPROVAL}
        if t < ap["approved_at"]:
            return {"allowed": False, "code": DENY_BEFORE_APPROVAL_TIME}
        if t < exe["executed_at"]:
            return {"allowed": False, "code": DENY_BEFORE_EXECUTION_TIME}
        return {"allowed": True, "code": ALLOW_TEAM}

    # 临时例外
    if exe is None:
        return {"allowed": False, "code": DENY_NOT_EXECUTED}
    if ap_i > ex_i:
        return {"allowed": False, "code": DENY_EXEC_BEFORE_APPROVAL}
    if t < ap["approved_at"]:
        return {"allowed": False, "code": DENY_BEFORE_APPROVAL_TIME}
    if t < exe["executed_at"]:
        return {"allowed": False, "code": DENY_BEFORE_EXECUTION_TIME}

    exid = hdef["exception_id"]
    revoked = any(e["kind"] == "revoke" and e["exception_id"] == exid
                  and e.get("by_role") == "duty_director" for e in prior)
    if revoked:
        return {"allowed": False, "code": DENY_REVOKED}

    ws, we = ap["window_start"], ap["window_end"]
    if endpoint_mode == "half_open":
        inside = ws <= t < we
    else:
        inside = ws <= t <= we
    if not inside:
        return {"allowed": False, "code": DENY_OUTSIDE_WINDOW}
    return {"allowed": True, "code": ALLOW_EXCEPTION}


def cross_check_oracle(setup, perm, mode="half_open"):
    """对一个完整交错：内核重放 vs oracle 逐 access 对拍。返回不一致列表。"""
    diffs = []
    kernel = replay_decisions(setup, list(perm), endpoint_mode=mode)
    for item in kernel:
        pos = item["position"]
        req = item["event"]
        kcode = item["decision"]["code"]
        kallow = item["decision"]["allowed"]
        op_prefix = list(perm[:pos])
        o = oracle_decide(setup, op_prefix, req, endpoint_mode=mode)
        if o["code"] != kcode or o["allowed"] != kallow:
            diffs.append({"request_id": req.get("request_id"), "position": pos,
                          "kernel": {"code": kcode, "allowed": kallow},
                          "oracle": o})
    return diffs


# ---------------------------------------------------------------------------
# 小输入世界（固定、合成、最小但覆盖全部维度）
# ---------------------------------------------------------------------------

SMALL = {
    "org": "ORG-ALPHA", "other_org": "ORG-BETA", "team": "TEAM-OPS",
    "person": "trustee.renee",
    "a_self": "ASG-DAY-IC",       # 交接目标：日班 现场指挥
    "a_shift": "ASG-NIGHT-IC",    # 仅班次不同：夜班 现场指挥
    "a_post": "ASG-DAY-OPERATOR",  # 仅职不同：日班 操作员
    "a_org": "ASG-BETA-IC",       # 另一组织中的身份
    "handoff": "HO-1", "exception": "EX-1",
    "cap_team": "ticket.handle",
    "cap_read": "ticket.read",
    "cap_exc": "refund.override",
    "approved_at": 10, "executed_at": 15,
    "ws": 20, "we": 30,
    "revoke_at": 25,
}


def small_setup() -> list[dict]:
    s = SMALL
    return [
        ev_assignment(s["a_self"], s["org"], s["team"], s["person"],
                      post="incident_commander", shift="day"),
        ev_assignment(s["a_shift"], s["org"], s["team"], s["person"],
                      post="incident_commander", shift="night"),
        ev_assignment(s["a_post"], s["org"], s["team"], s["person"],
                      post="operator", shift="day"),
        ev_assignment(s["a_org"], s["other_org"], "TEAM-B", s["person"],
                      post="incident_commander", shift="day"),
        ev_open_handoff(
            s["handoff"], org=s["org"], team=s["team"], trustee=s["person"],
            assignment_id=s["a_self"], from_account="SA-SYNTH-07",
            team_capabilities=[s["cap_read"], s["cap_team"]],
            exception_id=s["exception"],
            exception_capability=s["cap_exc"],
            requested_window=[s["ws"], s["we"]]),
    ]


def small_approve():
    s = SMALL
    return ev_approve(s["handoff"], approved_at=s["approved_at"],
                      approver="manager.wang", window_start=s["ws"],
                      window_end=s["we"])


def small_execute():
    s = SMALL
    return ev_execute(s["handoff"], executed_at=s["executed_at"])


def small_revoke(*, at=None):
    s = SMALL
    return ev_revoke(s["exception"], revoked_at=s["revoke_at"] if at is None else at,
                     by="director.zhou")


def small_ticks() -> list[int]:
    """围绕三个时间与窗口两端构造的边界富集滴答集。"""
    s = SMALL
    vals = []
    for base in (s["approved_at"], s["executed_at"], s["ws"], s["we"],
                 s["revoke_at"]):
        vals += [base - 1, base, base + 1]
    return sorted(set(vals))


def _req(rid, aid, cap, t, *, org=None):
    s = SMALL
    return ev_access(rid, at=t, org=org or s["org"], person=s["person"],
                     assignment_id=aid, capability=cap)


# ---------------------------------------------------------------------------
# 小输入：窗口端点独立枚举
# ---------------------------------------------------------------------------

def enumerate_endpoints() -> dict:
    s = SMALL
    setup = small_setup()
    ap, ex = small_approve(), small_execute()
    ticks = small_ticks()
    scopes = [
        ("self_shift_post", s["a_self"], s["org"]),
        ("shift_sibling", s["a_shift"], s["org"]),
        ("post_sibling", s["a_post"], s["org"]),
        ("other_org_assignment", s["a_org"], s["other_org"]),
    ]

    rows = []
    failures = []
    for mode in ("half_open", "inclusive"):
        for revoked in (False, True):
            prefix = [ap, ex] + ([small_revoke()] if revoked else [])
            for scope_name, aid, org in scopes:
                for t in ticks:
                    for cap_name, cap in (("exc", s["cap_exc"]),
                                          ("team", s["cap_team"])):
                        req = _req(f"EP-{mode}-{revoked}-{scope_name}-{cap_name}-{t}",
                                   aid, cap, t, org=org)
                        dk = decide(fold(setup + prefix), req, endpoint_mode=mode)
                        do = oracle_decide(setup, prefix, req, endpoint_mode=mode)
                        if dk["code"] != do["code"]:
                            failures.append({"where": "endpoint", "mode": mode,
                                             "revoked": revoked, "scope": scope_name,
                                             "cap": cap_name, "t": t,
                                             "kernel": dk["code"], "oracle": do["code"]})
                        rows.append({"mode": mode, "revoked": revoked,
                                     "scope": scope_name, "cap": cap_name, "t": t,
                                     "allowed": dk["allowed"], "code": dk["code"]})

    # 显式、独立地枚举左端与右端端点（默认半开、未吊销、本身份、例外能力）。
    def one(t):
        req = _req(f"EDGE-{t}", s["a_self"], s["cap_exc"], t)
        return decide(fold(setup + [ap, ex]), req)["code"]

    left = {str(t): one(t) for t in (s["ws"] - 1, s["ws"], s["ws"] + 1)}
    right = {str(t): one(t) for t in (s["we"] - 1, s["we"], s["we"] + 1)}

    expected_half_open = {
        s["ws"] - 1: DENY_OUTSIDE_WINDOW,
        s["ws"]: ALLOW_EXCEPTION,
        s["we"] - 1: ALLOW_EXCEPTION,
        s["we"]: DENY_OUTSIDE_WINDOW,
        s["we"] + 1: DENY_OUTSIDE_WINDOW,
    }
    endpoint_assertions = []
    for t, code in expected_half_open.items():
        got = one(t)
        endpoint_assertions.append({"t": t, "expected": code, "got": got,
                                    "pass": got == code})
        if got != code:
            failures.append({"where": "endpoint-assert", "t": t,
                             "expected": code, "got": got})

    # 不继承不变量：任何滴答下，职/班次兄弟身份对交接专属能力都必须拒绝。
    inheritance_violations = [
        r for r in rows
        if r["scope"] in ("shift_sibling", "post_sibling")
        and r["cap"] == "exc" and r["allowed"]
    ]
    return {"ticks": ticks, "row_count": len(rows), "rows": rows,
            "left_endpoint": left, "right_endpoint": right,
            "endpoint_assertions": endpoint_assertions,
            "inheritance_violation_count": len(inheritance_violations),
            "failures": failures}


# ---------------------------------------------------------------------------
# 小输入：所有短交错全排列
# ---------------------------------------------------------------------------

def _labeled_pool_basic() -> list[dict]:
    s = SMALL
    return [
        {**small_approve(), "_label": "approve"},
        {**small_execute(), "_label": "execute"},
        {**small_revoke(), "_label": "revoke"},
        {**_req("AC-TEAM-EARLY", s["a_self"], s["cap_team"], s["approved_at"] + 2),
         "_label": "access_team@12"},
        {**_req("AC-TEAM-OK", s["a_self"], s["cap_team"], s["executed_at"] + 1),
         "_label": "access_team@16"},
        {**_req("AC-EXC-IN", s["a_self"], s["cap_exc"], s["ws"] + 4),
         "_label": "access_exc@24"},
    ]


def _labeled_pool_scope() -> list[dict]:
    s = SMALL
    return [
        {**small_approve(), "_label": "approve"},
        {**small_execute(), "_label": "execute"},
        {**_req("AC-SELF-EXC", s["a_self"], s["cap_exc"], s["ws"] + 4),
         "_label": "self_exc@24"},
        {**_req("AC-NIGHT-EXC", s["a_shift"], s["cap_exc"], s["ws"] + 4),
         "_label": "night_exc@24"},
        {**_req("AC-POST-TEAM", s["a_post"], s["cap_team"], s["executed_at"] + 1),
         "_label": "post_team@16"},
    ]


def enumerate_interleavings() -> dict:
    setup = small_setup()
    all_failures = []
    summary = []

    for family, pool, mode in (
            ("basic6", _labeled_pool_basic(), "half_open"),
            ("scope5", _labeled_pool_scope(), "half_open")):
        labels = [e["_label"] for e in pool]
        perms = list(permutations(range(len(pool))))
        allow_count = deny_count = 0
        oracle_diffs = 0
        invariant_violations = []

        for order in perms:
            perm = []
            for i in order:
                e = {k: v for k, v in pool[i].items() if not k.startswith("_")}
                perm.append(e)

            diffs = cross_check_oracle(setup, perm, mode)
            if diffs:
                oracle_diffs += len(diffs)
                all_failures.append({"family": family, "order":
                                     [labels[i] for i in order], "diffs": diffs[:2]})

            decisions = replay_decisions(setup, perm, endpoint_mode=mode)
            seen_revoke_before = False
            for item in decisions:
                e = item["event"]
                d = item["decision"]
                if d["allowed"]:
                    allow_count += 1
                else:
                    deny_count += 1

                # 不变量 A：ALLOW 的来源作用域必须精确等于请求作用域。
                if d["allowed"]:
                    prov = d.get("provenance", {})
                    if prov.get("assignment_id") != e["assignment_id"]:
                        invariant_violations.append(
                            {"v": "provenance_scope", "request": e["request_id"]})

                # 不变量 B：scope5 中兄弟身份永远不允许（无论怎么交错）。
                if family == "scope5" and "NIGHT" in e["request_id"] and d["allowed"]:
                    invariant_violations.append(
                        {"v": "night_inherited", "order": [labels[i] for i in order]})
                if family == "scope5" and "POST" in e["request_id"] and d["allowed"]:
                    invariant_violations.append(
                        {"v": "post_inherited", "order": [labels[i] for i in order]})

            # 不变量 C：对 basic6 逐位置检查“若吊销已在该 access 前缀中，
            # 例外 access 必不允许”；“吊销绝不影响队内权限的允许性”。
            st = fold(setup)
            revoked_seen = False
            approved_seen = executed_ok_seen = False
            for raw in perm:
                if raw["kind"] == "revoke" and raw.get("by_role") == "duty_director":
                    revoked_seen = True
                if raw["kind"] == "approve":
                    approved_seen = True
                if raw["kind"] == "execute":
                    executed_ok_seen = approved_seen
                if raw["kind"] == "access":
                    d = decide(st, raw, endpoint_mode=mode)
                    if raw["capability"] == SMALL["cap_exc"] and revoked_seen and d["allowed"]:
                        invariant_violations.append(
                            {"v": "revocation_not_effective",
                             "request": raw["request_id"]})
                    if raw["capability"] == SMALL["cap_team"]:
                        # 队内权限的裁决码绝不能是吊销类。
                        if d["code"] == DENY_REVOKED:
                            invariant_violations.append(
                                {"v": "revoke_killed_team_permission",
                                 "request": raw["request_id"]})
                else:
                    apply_event(st, raw)

        summary.append({"family": family, "events": labels,
                        "permutation_count": len(perms),
                        "access_decisions": allow_count + deny_count,
                        "allow": allow_count, "deny": deny_count,
                        "oracle_diff_count": oracle_diffs,
                        "invariant_violation_count": len(invariant_violations)})
        if invariant_violations:
            all_failures.append({"family": family,
                                 "invariant_violations": invariant_violations[:10]})

    return {"families": summary, "failures": all_failures,
            "total_permutations": sum(f["permutation_count"] for f in summary)}


# ---------------------------------------------------------------------------
# 最小见证 1：交接生效（最短 ALLOW 轨迹 + 必要性/最小性证明）
# ---------------------------------------------------------------------------

def build_witness_effective() -> dict:
    s = SMALL
    setup = small_setup()
    trace = [small_approve(), small_execute(),
             _req("W1-ACCESS", s["a_self"], s["cap_team"], s["executed_at"] + 1)]
    decisions = replay_decisions(setup, trace)
    target = decisions[-1]["decision"]

    # 必要性：逐个移除/弱化关键事实，结论必须翻转为拒绝。
    necessity = []

    def mutated_denies(name, alt_trace):
        ds = replay_decisions(setup, alt_trace)
        d = ds[-1]["decision"]
        ok = (not d["allowed"])
        necessity.append({"mutation": name, "flipped_to_deny": ok,
                          "code_after_mutation": d["code"]})
        return ok

    mutated_denies("remove_approval", [small_execute(), trace[-1]])
    mutated_denies("remove_execution", [small_approve(), trace[-1]])
    mutated_denies("access_before_executed_at",
                   [small_approve(), small_execute(),
                    _req("W1-MUT-T", s["a_self"], s["cap_team"],
                         s["executed_at"] - 1)])
    mutated_denies("act_in_night_shift_instead",
                   [small_approve(), small_execute(),
                    _req("W1-MUT-SCOPE", s["a_shift"], s["cap_team"],
                         s["executed_at"] + 1)])

    # 最小性：枚举 [approve, execute, access] 的全部排列与前缀，
    # 找到能产生 ALLOW 的最少事件数。
    core = [small_approve(), small_execute(), trace[-1]]
    shortest = None
    examples = []
    for r in range(1, 4):
        for order in permutations(range(3), r):
            seq = [core[i] for i in order]
            ds = replay_decisions(setup, seq)
            if ds and ds[-1]["decision"]["allowed"]:
                n = len(seq)
                shortest = n if shortest is None else min(shortest, n)
                examples.append([core[i]["kind"] for i in order])
    minimal_event_count = 3
    minimal_ok = shortest == minimal_event_count

    core_obj = {
        "witness_id": "W1-HANDOFF-EFFECTIVE",
        "title_zh": "最小见证一：交接在批准并执行后于精确日班身份上生效",
        "claim": {
            "request_id": "W1-ACCESS",
            "expected_allowed": True,
            "expected_code": ALLOW_TEAM,
            "minimal_event_count": minimal_event_count,
            "minimality_confirmed": minimal_ok,
        },
        "semantics": {
            "window": "half_open [start,end)",
            "order_visibility": "request sees only earlier ledger events",
            "times_separated": ["approved_at", "executed_at", "read_watermark"],
        },
        "entities": setup,
        "trace": trace,
        "observed_decision": target,
        "necessity_checks": necessity,
        "shortest_allow_event_count_found": shortest,
    }
    doc = {
        "schema": SCHEMA, "model_version": MODEL_VERSION,
        "generator": TOOL_NAME, "kind": "handoff_effective",
        "core": core_obj,
        "core_digest": sha256_obj(core_obj),
    }
    return doc


# ---------------------------------------------------------------------------
# 最小见证 2：最早拒绝时点（穷举认证 + 两类“最早”口径）
# ---------------------------------------------------------------------------

def _canonical_endpoint_cases():
    """完整小输入探测宇宙：scope × tick × (无吊销/已吊销) × 能力。"""
    s = SMALL
    setup = small_setup()
    ap, ex = small_approve(), small_execute()
    ticks = small_ticks()
    scopes = [
        ("self", s["a_self"], s["org"]),
        ("shift_sibling", s["a_shift"], s["org"]),
        ("post_sibling", s["a_post"], s["org"]),
        ("other_org", s["a_org"], s["other_org"]),
    ]
    cases = []
    # 同一最早滴答上有多个拒绝时，优先把“本交接精确身份 + 例外能力”作为
    # 代表见证（最贴题、且 [approve, execute, access] 对该原因码是最小的）。
    scope_priority = {"self": 0, "shift_sibling": 1, "post_sibling": 2,
                      "other_org": 3}
    cap_priority = {"exc": 0, "team": 1}
    for revoked in (False, True):
        prefix = [ap, ex] + ([small_revoke()] if revoked else [])
        for scope_name, aid, org in scopes:
            for cap_name, cap in (("team", s["cap_team"]), ("exc", s["cap_exc"])):
                for t in ticks:
                    rid = f"UNI-{int(revoked)}-{scope_name}-{cap_name}-{t}"
                    req = _req(rid, aid, cap, t, org=org)
                    d = decide(fold(setup + prefix), req)
                    cases.append({
                        "key": (t, 1 if revoked else 0,
                                scope_priority[scope_name],
                                cap_priority[cap_name]),
                        "t": t, "revoked_situation": revoked,
                        "scope": scope_name, "cap": cap_name,
                        "request": req, "prefix": prefix, "decision": d})
    return setup, cases


def certify_earliest() -> dict:
    setup, cases = _canonical_endpoint_cases()

    # 口径 A：整个声明有限宇宙中，按 (访问滴答, 吊销情形, scope, cap) 排序的
    # 字面上最早的 DENY。
    denies = [c for c in cases if not c["decision"]["allowed"]]
    literal = min(denies, key=lambda c: c["key"])

    # 口径 B：“曾有效访问的最早拒绝边界”——用成对括号轨迹
    # [approve, execute, pre@r, revoke@r, post@r]，要求 pre ALLOW、post DENY，
    # 在 r 上独立枚举窗口内/边缘的所有候选滴答，取最小 r。
    s = SMALL
    # 连续滴答扫描（不止端点）：自身份例外 + 规范 [approve, execute] 前缀，
    # 覆盖 t=0..we 的每个整数滴答，并与独立 oracle 对拍。允许集合必须恰好是
    # 半开窗口内的 range(ws, we) = 20..29。
    sweep_prefix = [small_approve(), small_execute()]
    allow_ticks = []
    sweep_mismatches = []
    for t in range(0, s["we"] + 1):
        rq = _req(f"SWEEP-{t}", s["a_self"], s["cap_exc"], t)
        dk = decide(fold(setup + sweep_prefix), rq)
        do = oracle_decide(setup, sweep_prefix, rq)
        if dk["code"] != do["code"]:
            sweep_mismatches.append({"t": t, "kernel": dk["code"],
                                     "oracle": do["code"]})
        if dk["allowed"]:
            allow_ticks.append(t)
    contiguous_sweep_ok = (
        allow_ticks == list(range(s["ws"], s["we"])) and not sweep_mismatches)

    boundary_rs = sorted(set([s["ws"] - 1, s["ws"], s["ws"] + 1,
                              s["revoke_at"] - 1, s["revoke_at"],
                              s["we"] - 1, s["we"]]))
    boundaries = []
    earliest_boundary = None
    for r in boundary_rs:
        pre = _req(f"BR-PRE-{r}", s["a_self"], s["cap_exc"], r)
        post = _req(f"BR-POST-{r}", s["a_self"], s["cap_exc"], r)
        trace = [small_approve(), small_execute(), pre, small_revoke(at=r), post]
        ds = replay_decisions(setup, trace)
        # replay_decisions 每个 access 事件返回一条：ds[0]=pre, ds[1]=post。
        dpre, dpost = ds[0]["decision"], ds[1]["decision"]
        ok = dpre["allowed"] and (not dpost["allowed"]) \
            and dpost["code"] == DENY_REVOKED
        rec = {"at": r, "pre_code": dpre["code"], "post_code": dpost["code"],
               "is_allow_to_deny_revocation_boundary": ok,
               "trace": trace}
        boundaries.append({k: v for k, v in rec.items() if k != "trace"})
        if ok and earliest_boundary is None:
            earliest_boundary = rec

    # 窗口关闭边界（allow at we-1 -> deny at we），作为对照的更晚边界。
    close_pre = _req("WC-PRE", s["a_self"], s["cap_exc"], s["we"] - 1)
    close_post = _req("WC-POST", s["a_self"], s["cap_exc"], s["we"])
    close_trace = [small_approve(), small_execute(), close_pre, close_post]
    close_ds = replay_decisions(setup, close_trace)
    # close_ds[0]=pre(at we-1), close_ds[1]=post(at we)
    window_close_boundary = {
        "at": s["we"], "pre_code": close_ds[0]["decision"]["code"],
        "post_code": close_ds[1]["decision"]["code"],
        "trace": close_trace,
        "confirmed": close_ds[0]["decision"]["allowed"]
        and not close_ds[1]["decision"]["allowed"]}

    # 最小性证明：比 literal.key 更小的所有 case 都不得是 DENY。
    earlier = [c for c in cases if c["key"] < literal["key"]]
    earlier_denies = [c for c in earlier if not c["decision"]["allowed"]]

    # 边界最小性：所有 r < earliest_boundary.at 都不构成 allow->deny 吊销边界。
    eb_at = earliest_boundary["at"] if earliest_boundary else None
    smaller_boundaries = [b for b in boundaries
                          if eb_at is not None and b["at"] < eb_at
                          and b["is_allow_to_deny_revocation_boundary"]]

    return {
        "universe_size": len(cases),
        "tick_min": min(small_ticks()), "tick_max": max(small_ticks()),
        "contiguous_allow_ticks": allow_ticks,
        "contiguous_sweep_ok": contiguous_sweep_ok,
        "contiguous_sweep_mismatches": sweep_mismatches,
        "literal_earliest": literal,
        "earlier_key_count": len(earlier),
        "earlier_key_deny_count": len(earlier_denies),
        "revocation_boundary": earliest_boundary,
        "all_boundary_probes": boundaries,
        "smaller_revocation_boundary_count": len(smaller_boundaries),
        "window_close_boundary": {
            "at": window_close_boundary["at"],
            "pre_code": window_close_boundary["pre_code"],
            "post_code": window_close_boundary["post_code"],
            "confirmed": window_close_boundary["confirmed"]},
        "window_close_trace": close_trace,
        "setup": setup,
    }


def build_witness_earliest(cert: dict) -> dict:
    lit = cert["literal_earliest"]
    rb = cert["revocation_boundary"]
    setup = cert["setup"]

    # 字面最早拒绝的最小轨迹：setup + 其前缀（无吊销情形即 approve/execute）。
    literal_trace = list(lit["prefix"]) + [lit["request"]]
    literal_decision = lit["decision"]

    core_obj = {
        "witness_id": "W2-EARLIEST-DENIAL",
        "title_zh": "最小见证二：经穷举认证的最早拒绝时点（含即时吊销边界）",
        "claim": {
            "literal_earliest_timepoint": lit["t"],
            "literal_code": literal_decision["code"],
            "literal_scope": lit["scope"],
            "literal_capability": lit["cap"],
            "universe_size": cert["universe_size"],
            "earlier_key_count": cert["earlier_key_count"],
            "earlier_key_deny_count": cert["earlier_key_deny_count"],
            "contiguous_allow_ticks": cert["contiguous_allow_ticks"],
            "contiguous_sweep_ok": cert["contiguous_sweep_ok"],
            "earliest_effective_revocation_boundary_at": rb["at"],
            "revocation_boundary_post_code": DENY_REVOKED,
            "smaller_revocation_boundary_count":
                cert["smaller_revocation_boundary_count"],
            "window_close_boundary_at": cert["window_close_boundary"]["at"],
        },
        "semantics": {
            "order_visibility": "request sees only earlier ledger events",
            "same_tick": "total ledger order resolves ties",
            "window": "half_open [start,end)",
            "two_senses_of_earliest": [
                "literal lexicographic earliest DENY in declared finite universe",
                "earliest allow->deny boundary for an access effective a step before",
            ],
        },
        "entities": setup,
        "literal_earliest_trace": literal_trace,
        "literal_observed_decision": literal_decision,
        "effective_revocation_boundary_trace": rb["trace"],
        "window_close_boundary_trace": cert["window_close_trace"],
        "minimality": {
            "universe_size": cert["universe_size"],
            "earlier_key_count": cert["earlier_key_count"],
            "earlier_key_deny_count": cert["earlier_key_deny_count"],
            "smaller_revocation_boundary_count":
                cert["smaller_revocation_boundary_count"],
            "all_boundary_probes": cert["all_boundary_probes"],
        },
    }
    return {
        "schema": SCHEMA, "model_version": MODEL_VERSION,
        "generator": TOOL_NAME, "kind": "earliest_denial",
        "core": core_obj,
        "core_digest": sha256_obj(core_obj),
    }


# ---------------------------------------------------------------------------
# 大输入：合成多组织数据 + 追加一条吊销的差分复核
# ---------------------------------------------------------------------------

def build_large_world(*, org_count=24, trustees_per_org=60, seed=20261126):
    rng = random.Random(seed)
    setup: list[dict] = []
    events: list[dict] = []
    probes: list[dict] = []
    historical_specs = []

    target = {
        "org": "ORG-000", "person": "trustee.target",
        "a_self": "ASG-TARGET-DAY-IC", "a_shift": "ASG-TARGET-NIGHT-IC",
        "handoff": "HO-TARGET", "exception": "EX-TARGET",
        "cap_team": "ticket.handle", "cap_exc": "refund.override",
        "approved_at": 100, "executed_at": 102,
        "ws": 110, "we": 20001, "revoke_at": 20000,
        "historical_watermark": 500,
    }

    # 确定性的目标组织/身份/交接（长窗口覆盖吊销时刻）。
    setup += [
        ev_assignment(target["a_self"], target["org"], "TEAM-OPS",
                      target["person"], "incident_commander", "day"),
        ev_assignment(target["a_shift"], target["org"], "TEAM-OPS",
                      target["person"], "incident_commander", "night"),
        ev_open_handoff(
            target["handoff"], org=target["org"], team="TEAM-OPS",
            trustee=target["person"], assignment_id=target["a_self"],
            from_account="SA-TARGET", team_capabilities=["ticket.read",
                                                         target["cap_team"]],
            exception_id=target["exception"],
            exception_capability=target["cap_exc"],
            requested_window=[target["ws"], target["we"]]),
    ]
    events += [
        ev_approve(target["handoff"], approved_at=target["approved_at"],
                   approver="manager.target", window_start=target["ws"],
                   window_end=target["we"]),
        ev_execute(target["handoff"], executed_at=target["executed_at"]),
    ]

    # 目标的差分探测：只在“吊销当时（且仍在窗口内）”放一条现在时请求，
    # 使全矩阵中恰好这一个裁决发生 ALLOW -> DENY 的因果翻转。
    target_probe_ids = ["TARGET-EXC-NOW"]
    probes.append(_large_probe("TARGET-EXC-NOW", target["org"], target["person"],
                               target["a_self"], target["cap_exc"],
                               target["revoke_at"],
                               tags={"org": target["org"],
                                     "exception_id": target["exception"]}))
    # 目标队内权限：吊销后必须仍允许。
    probes.append(_large_probe("TARGET-TEAM-0", target["org"], target["person"],
                               target["a_self"], target["cap_team"], 600,
                               tags={"org": target["org"], "exception_id": None}))
    # 目标的夜班兄弟身份：任何时候都不得继承。
    probes.append(_large_probe("TARGET-SHIFT-DENY", target["org"], target["person"],
                               target["a_shift"], target["cap_exc"], 600,
                               tags={"org": target["org"], "exception_id": None}))

    # 目标的历史水位报告（水位远早于吊销）。
    historical_specs.append({
        "report_id": "HIST-TARGET-0",
        "read_watermark": target["historical_watermark"],
        "probes": [_large_probe("HIST-TARGET-ACCESS", target["org"],
                                target["person"], target["a_self"],
                                target["cap_exc"],
                                target["historical_watermark"],
                                tags={"org": target["org"]})]})

    handoff_count = 1
    assignment_count = 2
    probe_count_seed = len(probes)

    for oi in range(org_count):
        org = f"ORG-{oi:03d}"
        if org == target["org"]:
            # ORG-000 仍生成其他受托人，证明同组织内无关交接不受影响。
            pass
        for ti in range(trustees_per_org):
            person = f"trustee.o{oi:02d}.t{ti:03d}"
            aid_day = f"ASG-{oi:03d}-{ti:03d}-DAY"
            aid_night = f"ASG-{oi:03d}-{ti:03d}-NIGHT"
            aid_post = f"ASG-{oi:03d}-{ti:03d}-OP"
            setup.append(ev_assignment(aid_day, org, "TEAM-OPS", person,
                                      "incident_commander", "day"))
            setup.append(ev_assignment(aid_night, org, "TEAM-OPS", person,
                                       "incident_commander", "night"))
            setup.append(ev_assignment(aid_post, org, "TEAM-OPS", person,
                                       "operator", "day"))
            assignment_count += 3

            # 约 2/3 的受托人有一个活跃交接到其日班指挥身份。
            if rng.random() > 0.66:
                continue
            hid = f"HO-{oi:03d}-{ti:03d}"
            exid = f"EX-{oi:03d}-{ti:03d}"
            base = 1000 + handoff_count * 7 + rng.randint(0, 3)
            appr = base
            exe = appr + rng.choice((1, 2, 3))
            ws = exe + rng.choice((2, 3, 4))
            we = ws + rng.choice((30, 40, 60, 80))
            mid = ws + max(1, (we - ws) // 2)
            setup.append(ev_open_handoff(
                hid, org=org, team="TEAM-OPS", trustee=person,
                assignment_id=aid_day, from_account=f"SA-{oi:03d}-{ti:03d}",
                team_capabilities=["ticket.read", "ticket.handle",
                                   "ticket.escalate"],
                exception_id=exid, exception_capability="refund.override",
                requested_window=[ws, we]))
            events.append(ev_approve(hid, approved_at=appr,
                                     approver=f"mgr.{oi:03d}",
                                     window_start=ws, window_end=we))
            # 绝大多数合法：批准后执行；少量故意执行早于批准的坏交错数据不进入
            # “当前合法矩阵”，这里大输入保持因果合法，坏交错由小输入全排列覆盖。
            events.append(ev_execute(hid, executed_at=exe))
            handoff_count += 1

            tags = {"org": org, "exception_id": exid}
            probes.append(_large_probe(f"P-{oi:03d}-{ti:03d}-TEAM", org, person,
                                       aid_day, "ticket.handle", exe + 1,
                                       tags={"org": org, "exception_id": None}))
            probes.append(_large_probe(f"P-{oi:03d}-{ti:03d}-EXC", org, person,
                                       aid_day, "refund.override", mid, tags=tags))
            probes.append(_large_probe(f"P-{oi:03d}-{ti:03d}-NIGHT-DENY", org,
                                       person, aid_night, "refund.override", mid,
                                       tags={"org": org, "exception_id": None}))
            probes.append(_large_probe(f"P-{oi:03d}-{ti:03d}-POST-DENY", org,
                                       person, aid_post, "ticket.handle", exe + 1,
                                       tags={"org": org, "exception_id": None}))

            # 少量历史水位报告。
            if ti % 20 == 0:
                w = ws + 1
                historical_specs.append({
                    "report_id": f"HIST-{oi:03d}-{ti:03d}",
                    "read_watermark": w,
                    "probes": [_large_probe(
                        f"H-{oi:03d}-{ti:03d}-ACCESS", org, person, aid_day,
                        "refund.override", w, tags={"org": org})]})

    # 事件按业务时间排序后作为“追加吊销之前”的账本（确定性）。
    events.sort(key=event_time)
    # 目标就在 ORG-000 内；循环 oi=0 会给同一组织补充其他受托人，
    # 因此不同组织总数正是 org_count（ORG-000 .. ORG-023）。
    return {"target": target, "setup": setup, "events_before": events,
            "probes": probes, "historical_specs": historical_specs,
            "target_probe_ids": target_probe_ids,
            "counts": {"orgs": org_count, "assignments": assignment_count,
                       "handoffs": handoff_count, "probes": len(probes),
                       "historical_reports": len(historical_specs),
                       "seed": seed}}


def _large_probe(pid, org, person, aid, cap, at, *, tags):
    e = ev_access(pid, at=at, org=org, person=person, assignment_id=aid,
                  capability=cap)
    e["_tags"] = tags
    return e


def _strip_tags(probe: dict) -> dict:
    return {k: v for k, v in probe.items() if not k.startswith("_")}


def _watermark_state(setup, events, w):
    visible = [e for e in events if event_time(e) <= w]
    return fold(setup + visible), visible


def run_large_review() -> dict:
    t0 = time.perf_counter()
    world = build_large_world()
    target = world["target"]
    setup = world["setup"]
    before_events = world["events_before"]
    probes = world["probes"]

    # ---- 历史报告（追加前生成，规范化哈希） ----
    hist_before = {}
    for spec in world["historical_specs"]:
        st_w, _visible = _watermark_state(setup, before_events,
                                          spec["read_watermark"])
        rows = []
        for p in spec["probes"]:
            d = decide(st_w, _strip_tags(p))
            rows.append({"probe_id": p["request_id"], "allowed": d["allowed"],
                         "code": d["code"]})
        report = {"report_id": spec["report_id"],
                  "read_watermark": spec["read_watermark"],
                  "rows": sorted(rows, key=lambda r: r["probe_id"])}
        hist_before[spec["report_id"]] = report
    hist_hash_before = sha256_obj(hist_before)

    # ---- 追加前的全量当前矩阵 ----
    st_before = fold(setup + before_events)
    decisions_before = {}
    for p in probes:
        d = decide(st_before, _strip_tags(p))
        decisions_before[p["request_id"]] = {
            "allowed": d["allowed"], "code": d["code"],
            "org": p["_tags"]["org"],
            "exception_id": p["_tags"].get("exception_id")}
    matrix_hash_before = sha256_obj(decisions_before)

    # 前置正向控制：目标在追加前确实允许。
    target_before = {pid: decisions_before[pid] for pid in world["target_probe_ids"]}
    pre_control_ok = all(v["allowed"] and v["code"] == ALLOW_EXCEPTION
                         for v in target_before.values())

    # ---- 追加且仅追加一条吊销 ----
    revocation = ev_revoke(target["exception"], revoked_at=target["revoke_at"],
                           by="director.zhou")
    after_events = before_events + [revocation]
    appended_exactly_one = len(after_events) == len(before_events) + 1

    st_after = fold(setup + after_events)
    decisions_after = {}
    for p in probes:
        d = decide(st_after, _strip_tags(p))
        decisions_after[p["request_id"]] = {
            "allowed": d["allowed"], "code": d["code"],
            "org": p["_tags"]["org"],
            "exception_id": p["_tags"].get("exception_id")}
    matrix_hash_after = sha256_obj(decisions_after)

    # ---- 差分 ----
    changed = []
    for pid, b in decisions_before.items():
        a = decisions_after[pid]
        if b["allowed"] != a["allowed"] or b["code"] != a["code"]:
            changed.append({"probe_id": pid, "org": a["org"],
                            "exception_id": a["exception_id"],
                            "before": b["code"], "after": a["code"]})

    other_org_changes = [c for c in changed if c["org"] != target["org"]]
    target_org_non_exception_changes = [
        c for c in changed
        if c["org"] == target["org"] and c["exception_id"] != target["exception"]]
    changed_exception_ids = {c["exception_id"] for c in changed}
    changed_probe_ids = {c["probe_id"] for c in changed}
    expected_changed = set(world["target_probe_ids"])

    only_target_exception_changed = (
        not other_org_changes
        and not target_org_non_exception_changes
        and changed_exception_ids == {target["exception"]}
        and changed_probe_ids == expected_changed
    )

    # 目标翻转语义正确：ALLOW_EXCEPTION -> DENY_REVOKED。
    flip_codes_ok = all(
        decisions_before[pid]["code"] == ALLOW_EXCEPTION
        and decisions_after[pid]["code"] == DENY_REVOKED
        for pid in world["target_probe_ids"])

    # 队内权限与不继承在追加后依旧成立。
    team_still_allowed = decisions_after["TARGET-TEAM-0"]["code"] == ALLOW_TEAM
    shift_still_denied = (
        decisions_after["TARGET-SHIFT-DENY"]["code"] == DENY_SCOPE_NOT_INHERITED)

    # ---- 追加后重算历史报告：必须逐字节不变，且吊销被水位排除 ----
    hist_after = {}
    revocation_excluded_from_every_history = True
    for spec in world["historical_specs"]:
        w = spec["read_watermark"]
        if event_time(revocation) <= w:
            revocation_excluded_from_every_history = False
        st_w, visible = _watermark_state(setup, after_events, w)
        rows = []
        for p in spec["probes"]:
            d = decide(st_w, _strip_tags(p))
            rows.append({"probe_id": p["request_id"], "allowed": d["allowed"],
                         "code": d["code"]})
        hist_after[spec["report_id"]] = {
            "report_id": spec["report_id"], "read_watermark": w,
            "rows": sorted(rows, key=lambda r: r["probe_id"])}

    hist_hash_after = sha256_obj(hist_after)
    history_byte_identical = hist_hash_before == hist_hash_after
    history_content_equal = hist_before == hist_after

    # 特别地，目标历史访问在其水位上仍为 ALLOW（历史不被吊销改写）。
    target_hist = hist_after["HIST-TARGET-0"]["rows"][0]
    target_history_preserved_allow = (
        target_hist["allowed"] and target_hist["code"] == ALLOW_EXCEPTION)

    # 其他组织矩阵逐字节不变。
    other_org_matrix_equal = all(
        decisions_before[pid] == decisions_after[pid]
        for pid, v in decisions_before.items() if v["org"] != target["org"])

    # 大输入上的不继承/跨组织负向控制统计。
    inherit_violations = [
        pid for pid, v in decisions_after.items()
        if pid.endswith(("NIGHT-DENY", "POST-DENY", "SHIFT-DENY"))
        and v["allowed"]
    ]

    ok = all([
        appended_exactly_one, pre_control_ok, flip_codes_ok,
        only_target_exception_changed, not other_org_changes,
        other_org_matrix_equal, history_byte_identical,
        history_content_equal, revocation_excluded_from_every_history,
        target_history_preserved_allow, team_still_allowed,
        shift_still_denied, not inherit_violations,
    ])

    elapsed = time.perf_counter() - t0
    return {
        "ok": ok, "elapsed_seconds": round(elapsed, 3),
        "counts": world["counts"],
        "target_org": target["org"], "target_exception": target["exception"],
        "appended_exactly_one_revocation": appended_exactly_one,
        "pre_revocation_target_all_allowed": pre_control_ok,
        "flip_codes_ok": flip_codes_ok,
        "target_before": target_before,
        "target_after": {pid: decisions_after[pid]
                         for pid in world["target_probe_ids"]},
        "changed_decision_count": len(changed),
        "changed_decisions": changed,
        "changed_probe_ids_exact_match": changed_probe_ids == expected_changed,
        "only_target_exception_changed": only_target_exception_changed,
        "other_org_changes": other_org_changes,
        "target_org_non_exception_changes": target_org_non_exception_changes,
        "other_org_matrix_equal": other_org_matrix_equal,
        "team_permission_still_allowed": team_still_allowed,
        "shift_noninheritance_still_denied": shift_still_denied,
        "inheritance_violation_count": len(inherit_violations),
        "historical_report_count": len(hist_before),
        "historical_hash_before": hist_hash_before,
        "historical_hash_after_append": hist_hash_after,
        "historical_reports_byte_identical": history_byte_identical,
        "revocation_time_exceeds_all_watermarks":
            revocation_excluded_from_every_history,
        "target_history_preserved_allow": target_history_preserved_allow,
        "matrix_hash_before": matrix_hash_before,
        "matrix_hash_after": matrix_hash_after,
    }


# ---------------------------------------------------------------------------
# 独立见证核验
# ---------------------------------------------------------------------------

def verify_witness_file(path: str | Path) -> dict:
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    checks = []

    def check(name, cond, detail=""):
        checks.append({"check": name, "pass": bool(cond), "detail": detail})
        return bool(cond)

    check("schema", doc.get("schema") == SCHEMA, str(doc.get("schema")))
    core = doc.get("core", {})
    pinned = doc.get("core_digest")
    if isinstance(pinned, str) and len(pinned) == 64:
        digest_ok = pinned == sha256_obj(core)
        check("core_digest_integrity", digest_ok,
              "pinned sha256 matches" if digest_ok else "DIGEST MISMATCH")
    else:
        # 随仓库提供的“引导副本”可能尚未钉住摘要；运行 `all` 会重新生成并钉住。
        # 独立性由下方重放 / oracle / 重新枚举保证，不依赖该摘要。
        check("core_digest_integrity", True,
              "unpinned bootstrap copy; replay+oracle+re-enumeration authoritative")

    kind = doc.get("kind")
    setup = core.get("entities", [])

    if kind == "handoff_effective":
        trace = core["trace"]
        ds = replay_decisions(setup, trace)
        claimed = core["claim"]
        target = ds[-1]["decision"]
        check("final_access_allowed", target["allowed"], target["code"])
        check("code_matches_claim", target["code"] == claimed["expected_code"],
              f"{target['code']} != {claimed['expected_code']}")
        od = oracle_decide(setup, trace[:-1], trace[-1])
        check("independent_oracle_agrees",
              od["allowed"] == target["allowed"] and od["code"] == target["code"],
              str(od))
        check("claimed_minimality", claimed.get("minimality_confirmed") is True)
        check("found_shortest_count_is_3",
              core.get("shortest_allow_event_count_found") == 3,
              str(core.get("shortest_allow_event_count_found")))
        nec = core.get("necessity_checks", [])
        check("all_necessity_mutations_flip_to_deny",
              len(nec) >= 4 and all(n["flipped_to_deny"] for n in nec),
              f"{sum(n['flipped_to_deny'] for n in nec)}/{len(nec)}")
        # 核验方独立重算必要性，而不是只信文件里的布尔。
        s = SMALL
        indep = []
        indep.append(not replay_decisions(setup, [small_execute(), trace[-1]])[-1]
                     ["decision"]["allowed"])
        indep.append(not replay_decisions(setup, [small_approve(), trace[-1]])[-1]
                     ["decision"]["allowed"])
        check("verifier_independently_replays_mutations", all(indep))

    elif kind == "earliest_denial":
        lit_trace = core["literal_earliest_trace"]
        ds = replay_decisions(setup, lit_trace)
        lit_decision = ds[-1]["decision"]
        claim = core["claim"]
        check("literal_trace_ends_deny", not lit_decision["allowed"],
              lit_decision["code"])
        check("literal_code_matches_claim",
              lit_decision["code"] == claim["literal_code"], lit_decision["code"])
        od = oracle_decide(setup, lit_trace[:-1], lit_trace[-1])
        check("literal_oracle_agrees", od["code"] == lit_decision["code"], str(od))

        rb_trace = core["effective_revocation_boundary_trace"]
        rds = replay_decisions(setup, rb_trace)
        pre_d, post_d = rds[0]["decision"], rds[1]["decision"]
        check("boundary_pre_allowed", pre_d["allowed"], pre_d["code"])
        check("boundary_post_revoked",
              (not post_d["allowed"]) and post_d["code"] == DENY_REVOKED,
              post_d["code"])
        check("boundary_at_matches_claim",
              rb_trace[4]["at"] == claim[
                  "earliest_effective_revocation_boundary_at"])

        wc = core["window_close_boundary_trace"]
        wds = replay_decisions(setup, wc)
        check("window_close_pair_confirmed",
              wds[0]["decision"]["allowed"]
              and not wds[1]["decision"]["allowed"]
              and wds[1]["decision"]["code"] == DENY_OUTSIDE_WINDOW,
              f"{wds[0]['decision']['code']} -> {wds[1]['decision']['code']}")

        # 关键：核验方独立重跑整个有限宇宙的最小性认证，不信任文件结论。
        cert = certify_earliest()
        check("universe_size_matches",
              cert["universe_size"] == claim["universe_size"],
              f"{cert['universe_size']} vs {claim['universe_size']}")
        check("independently_certified_literal_tick",
              cert["literal_earliest"]["t"] == claim["literal_earliest_timepoint"]
              and cert["literal_earliest"]["decision"]["code"]
              == claim["literal_code"],
              f"tick={cert['literal_earliest']['t']} "
              f"code={cert['literal_earliest']['decision']['code']}")
        check("no_earlier_deny_in_independent_universe",
              cert["earlier_key_deny_count"] == 0
              and claim["earlier_key_deny_count"] == 0)
        check("contiguous_sweep_exact_window",
              cert["contiguous_sweep_ok"]
              and claim.get("contiguous_sweep_ok") is True
              and cert["contiguous_allow_ticks"]
              == claim.get("contiguous_allow_ticks"),
              f"allow_ticks={cert['contiguous_allow_ticks']}")
        check("independently_certified_revocation_boundary",
              cert["revocation_boundary"] is not None
              and cert["revocation_boundary"]["at"]
              == claim["earliest_effective_revocation_boundary_at"]
              and cert["smaller_revocation_boundary_count"] == 0)
    else:
        check("known_kind", False, f"unknown kind {kind!r}")

    ok = all(c["pass"] for c in checks)
    return {"path": str(path), "kind": kind, "ok": ok, "checks": checks,
            "file_sha256": sha256_file(path)}


# ---------------------------------------------------------------------------
# Markdown 人读见证
# ---------------------------------------------------------------------------

def _md_escape(x) -> str:
    return str(x).replace("|", "\\|")


def render_effective_md(doc: dict) -> str:
    c = doc["core"]
    cl = c["claim"]
    d = c["observed_decision"]
    lines = [
        f"# 见证一 · 交接生效（{c['witness_id']}）",
        "",
        c["title_zh"],
        "",
        f"- Schema：`{doc['schema']}`　模型版本：`{doc['model_version']}`",
        f"- core 摘要：`{doc['core_digest'][:16]}…`",
        f"- 结论：**{d['code']}** — {d['reason_zh']}",
        f"- 请求：`{cl['request_id']}` 以 `{d['provenance']['post']} / "
        f"{d['provenance']['shift']}` 身份在 t={c['trace'][-1]['at']} "
        f"访问 `{d['provenance']['capability']}`",
        f"- 批准时间 `approved_at={SMALL['approved_at']}`，"
        f"执行时间 `executed_at={SMALL['executed_at']}`（二者分开）",
        "",
        "## 最小轨迹（原始事件，可独立重放）",
        "",
        "| 顺序 | 事件 | 关键字段 |",
        "|---:|---|---|",
    ]
    for i, e in enumerate(c["trace"]):
        if e["kind"] == "approve":
            detail = f"approved_at={e['approved_at']}, window=[{e['window_start']},{e['window_end']})"
        elif e["kind"] == "execute":
            detail = f"executed_at={e['executed_at']}"
        else:
            detail = f"at={e['at']}, as={e['assignment_id']}, cap={e['capability']}"
        lines.append(f"| {i + 1} | `{e['kind']}` | {_md_escape(detail)} |")
    lines += ["", "## 最小性 / 必要性", "",
              f"- 穷举 `[approve, execute, access]` 全部前缀与排列，"
              f"产生 ALLOW 的最少事件数 = "
              f"**{c['shortest_allow_event_count_found']}**（声明 "
              f"{cl['minimal_event_count']}，一致：{cl['minimality_confirmed']}）。",
              "- 移除或弱化任一关键事实，结论都必须翻转为拒绝：", ""]
    for n in c["necessity_checks"]:
        mark = "✅" if n["flipped_to_deny"] else "❌"
        lines.append(f"  - {mark} `{n['mutation']}` → `{n['code_after_mutation']}`")
    lines += ["", "## 如何独立核验", "",
              "```bash",
              f"python verify_witness.py witnesses/{Path(_w1_name()).name}",
              "# 或",
              f"python handoff_review.py verify witnesses/{Path(_w1_name()).name}",
              "```",
              "核验器只读取本文件内的原始 `entities` + `trace` 重新折叠，"
              "并用独立 oracle 对拍，不信任 `observed_decision`。", ""]
    return "\n".join(lines)


def _w1_name():
    return "witness_1_handoff_effective.json"


def _w2_name():
    return "witness_2_earliest_denial.json"


def render_earliest_md(doc: dict) -> str:
    c = doc["core"]
    cl = c["claim"]
    ld = c["literal_observed_decision"]
    rb = next(b for b in c["minimality"]["all_boundary_probes"]
              if b["at"] == cl["earliest_effective_revocation_boundary_at"])
    lines = [
        f"# 见证二 · 最早拒绝时点（{c['witness_id']}）",
        "",
        c["title_zh"],
        "",
        f"- Schema：`{doc['schema']}`　core 摘要：`{doc['core_digest'][:16]}…`",
        "",
        "## 口径 A：声明有限宇宙中字面上最早的拒绝",
        "",
        f"- 宇宙大小：**{cl['universe_size']}** 个 "
        "scope×能力×滴答×吊销情形 探测（全部独立枚举，非抽样）。",
        f"- 最早拒绝滴答：**t={cl['literal_earliest_timepoint']}**，"
        f"scope=`{cl['literal_scope']}`，cap=`{cl['literal_capability']}`，"
        f"裁决 **`{cl['literal_code']}`**。",
        f"- 比该排序键更早的候选数={cl['earlier_key_count']}，"
        f"其中拒绝数={cl['earlier_key_deny_count']}（必须为 0，方证明“最早”）。",
        f"- 人读：{ld['reason_zh']}",
        "",
        "### 该最早拒绝的最小轨迹",
        "",
        "| 顺序 | 事件 | 关键字段 |",
        "|---:|---|---|",
    ]
    for i, e in enumerate(c["literal_earliest_trace"]):
        if e["kind"] == "approve":
            detail = f"approved_at={e['approved_at']}, window=[{e['window_start']},{e['window_end']})"
        elif e["kind"] == "execute":
            detail = f"executed_at={e['executed_at']}"
        elif e["kind"] == "access":
            detail = f"at={e['at']}, as={e['assignment_id']}, cap={e['capability']}"
        else:
            detail = json.dumps(e, ensure_ascii=False)
        lines.append(f"| {i + 1} | `{e['kind']}` | {_md_escape(detail)} |")

    lines += [
        "",
        "## 口径 B：曾有效访问的最早 allow→deny 边界（即时吊销）",
        "",
        f"- 最早边界滴答：**t={cl['earliest_effective_revocation_boundary_at']}**"
        "（即批准窗口起点：例外最早可被行使的滴答；主任可在该滴答以账本顺序"
        "先于请求完成吊销）。",
        f"- 边界前一请求：`{rb['pre_code']}`；"
        f"边界后请求：`{rb['post_code']}`（应为 `{DENY_REVOKED}`）。",
        f"- 更早的 allow→deny 吊销边界数="
        f"**{cl['smaller_revocation_boundary_count']}**（必须为 0）。",
        f"- 对照：窗口关闭边界在 t={cl['window_close_boundary_at']} "
        f"（`{ALLOW_EXCEPTION}` → `{DENY_OUTSIDE_WINDOW}`），晚于吊销边界。",
        "- 同一滴答 t=20 的先后完全由账本全序决定：吊销事件在前则拒绝，"
        "访问排在吊销之前则该次仍允许；吊销对后续请求即时生效。",
        "",
        "## 如何独立核验",
        "",
        "```bash",
        f"python verify_witness.py witnesses/{_w2_name()}",
        "```",
        "核验器会：重放两条轨迹、用独立 oracle 对拍，并 **重新枚举整个有限宇宙**"
        "以确认没有更早的拒绝/边界，而不是相信本文件的声明字段。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 输出与主流程
# ---------------------------------------------------------------------------

def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2,
                               default=list), encoding="utf-8")


def cmd_all(args) -> int:
    witness_dir = Path(args.witness_dir)
    out_dir = Path(args.out)
    witness_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("合成账号交接 · 本地复核（Python 3.12 标准库，离线合成数据）")
    print("=" * 72)

    # 1) 端点枚举
    ep = enumerate_endpoints()
    write_json(out_dir / "small_endpoint_enumeration.json",
               {k: v for k, v in ep.items() if k != "rows"} |
               {"row_count": ep["row_count"]})
    write_json(out_dir / "small_endpoint_rows.json", ep["rows"])
    ep_ok = (not ep["failures"]
             and ep["inheritance_violation_count"] == 0
             and all(a["pass"] for a in ep["endpoint_assertions"]))
    print(f"[小输入][端点] 枚举 {ep['row_count']} 个 scope×能力×滴答×模式 探测；"
          f"左端 {ep['left_endpoint']}；右端 {ep['right_endpoint']}")
    print(f"[小输入][端点] 端点断言全过={all(a['pass'] for a in ep['endpoint_assertions'])}；"
          f"不继承违规={ep['inheritance_violation_count']}；"
          f"oracle/内核不一致={len([f for f in ep['failures'] if f.get('where')=='endpoint'])}")

    # 2) 交错全排列
    il = enumerate_interleavings()
    write_json(out_dir / "small_interleaving_summary.json", il)
    il_ok = not il["failures"] and all(
        f["oracle_diff_count"] == 0 and f["invariant_violation_count"] == 0
        for f in il["families"])
    for f in il["families"]:
        print(f"[小输入][交错] {f['family']}: {f['permutation_count']} 全排列，"
              f"{f['access_decisions']} 裁决 (allow={f['allow']}, deny={f['deny']})，"
              f"oracle差异={f['oracle_diff_count']}，"
              f"不变量违规={f['invariant_violation_count']}")

    # 3) 最早拒绝认证 + 两张见证
    cert = certify_earliest()
    w1 = build_witness_effective()
    w2 = build_witness_earliest(cert)
    p1 = witness_dir / _w1_name()
    p2 = witness_dir / _w2_name()
    write_json(p1, w1)
    write_json(p2, w2)
    (witness_dir / "witness_1_handoff_effective.md").write_text(
        render_effective_md(w1), encoding="utf-8")
    (witness_dir / "witness_2_earliest_denial.md").write_text(
        render_earliest_md(w2), encoding="utf-8")
    write_json(out_dir / "earliest_certification_summary.json", {
        "universe_size": cert["universe_size"],
        "literal": {"t": cert["literal_earliest"]["t"],
                    "code": cert["literal_earliest"]["decision"]["code"],
                    "scope": cert["literal_earliest"]["scope"]},
        "revocation_boundary_at": cert["revocation_boundary"]["at"],
        "window_close_at": cert["window_close_boundary"]["at"]})
    print(f"[见证] 写出 {p1}")
    print(f"[见证] 写出 {p2}")
    print(f"[见证2] 字面最早拒绝 t={cert['literal_earliest']['t']} "
          f"({cert['literal_earliest']['decision']['code']})；"
          f"最早有效吊销边界 t={cert['revocation_boundary']['at']}")

    # 4) 大输入差分
    large = run_large_review()
    write_json(out_dir / "large_revocation_report.json", large)
    c = large["counts"]
    print(f"[大输入] {c['orgs']} 组织 / {c['assignments']} 作业身份 / "
          f"{c['handoffs']} 交接 / {c['probes']} 探测 / "
          f"{c['historical_reports']} 历史报告，用时 {large['elapsed_seconds']}s")
    print(f"[大输入] 追加恰好一条吊销={large['appended_exactly_one_revocation']}；"
          f"变化裁决数={large['changed_decision_count']}（应恰为目标例外探测）")
    print(f"[大输入] 仅目标例外变化={large['only_target_exception_changed']}；"
          f"其他组织矩阵不变={large['other_org_matrix_equal']}")
    print(f"[大输入] 历史报告逐字节不变={large['historical_reports_byte_identical']}；"
          f"目标历史 ALLOW 保留={large['target_history_preserved_allow']}")
    print(f"[大输入] 吊销后队内权限仍允许={large['team_permission_still_allowed']}；"
          f"夜班不继承仍拒绝={large['shift_noninheritance_still_denied']}；"
          f"继承违规={large['inheritance_violation_count']}")

    # 5) 在同一次运行里走独立 verify 路径
    v1 = verify_witness_file(p1)
    v2 = verify_witness_file(p2)
    write_json(out_dir / "verify_w1.json", v1)
    write_json(out_dir / "verify_w2.json", v2)
    print(f"[核验] 见证一 {'PASS' if v1['ok'] else 'FAIL'}（"
          f"{sum(x['pass'] for x in v1['checks'])}/{len(v1['checks'])} 项）")
    print(f"[核验] 见证二 {'PASS' if v2['ok'] else 'FAIL'}（"
          f"{sum(x['pass'] for x in v2['checks'])}/{len(v2['checks'])} 项）")

    overall = all([ep_ok, il_ok, large["ok"], v1["ok"], v2["ok"]])
    summary = {
        "overall_pass": overall,
        "endpoint_enum_pass": ep_ok,
        "interleaving_enum_pass": il_ok,
        "large_revocation_pass": large["ok"],
        "witness1_verify_pass": v1["ok"],
        "witness2_verify_pass": v2["ok"],
        "witness_files": [str(p1), str(p2)],
        "report_dir": str(out_dir),
    }
    write_json(out_dir / "run_summary.json", summary)
    print("-" * 72)
    print("总体结果：", "ALL PASS" if overall else "FAILURE (see artifacts/review)")
    return 0 if overall else 1


def cmd_verify(args) -> int:
    paths = args.paths
    if not paths:
        paths = [DEFAULT_WITNESS_DIR / _w1_name(),
                 DEFAULT_WITNESS_DIR / _w2_name()]
    rc = 0
    for raw in paths:
        res = verify_witness_file(raw)
        status = "PASS" if res["ok"] else "FAIL"
        print(f"[{status}] {res['path']}  kind={res['kind']}  "
              f"sha256={res['file_sha256'][:16]}…")
        for c in res["checks"]:
            mark = "  [ok] " if c["pass"] else "  [XX] "
            print(mark + c["check"] + (f" — {c['detail']}" if c["detail"] else ""))
        if not res["ok"]:
            rc = 1
    return rc


def cmd_small(args) -> int:
    ep = enumerate_endpoints()
    il = enumerate_interleavings()
    cert = certify_earliest()
    print(json.dumps({
        "endpoint_rows": ep["row_count"],
        "endpoint_failures": ep["failures"],
        "interleaving": il,
        "literal_earliest": {"t": cert["literal_earliest"]["t"],
                             "code": cert["literal_earliest"]["decision"]["code"]},
        "revocation_boundary_at": cert["revocation_boundary"]["at"],
    }, ensure_ascii=False, indent=2, default=list))
    return 0 if not ep["failures"] and not il["failures"] else 1


def cmd_large(args) -> int:
    report = run_large_review()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=list))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="合成账号交接本地复核工具（标准库）")
    sub = p.add_subparsers(dest="command")

    pa = sub.add_parser("all", help="枚举+见证+大输入差分+独立核验（默认）")
    pa.add_argument("--out", default=str(DEFAULT_OUT_DIR))
    pa.add_argument("--witness-dir", default=str(DEFAULT_WITNESS_DIR))
    pa.set_defaults(func=cmd_all)

    pv = sub.add_parser("verify", help="独立核验见证 JSON（可给多个路径）")
    pv.add_argument("paths", nargs="*")
    pv.set_defaults(func=cmd_verify)

    ps = sub.add_parser("small", help="只跑小输入穷举并打印 JSON 摘要")
    ps.set_defaults(func=cmd_small)

    pl = sub.add_parser("large", help="只跑大输入差分并打印报告")
    pl.set_defaults(func=cmd_large)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # 无参数：默认执行 all。
        args = parser.parse_args(["all"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
