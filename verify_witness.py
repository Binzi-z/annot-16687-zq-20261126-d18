#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_witness.py — 两张最小见证的独立核验入口（仅 Python 3.12 标准库）

用法（Windows 原生）：
    py -3.12 verify_witness.py
    python verify_witness.py witnesses\\witness_1_handoff_effective.json
    python verify_witness.py witnesses\\witness_2_earliest_denial.json

不带参数时核验 witnesses/ 下的两张默认见证。

“独立”含义：本入口不调用生成见证的 build_* 函数，只把见证 JSON 中的原始
entities/trace 交给纯裁决内核与另写的 oracle 重新折叠/重放；见证二还会重新
枚举整个有限宇宙来证明“最早”。文件中记录的 verdict/minimality 布尔一律不被
信任，只用于与重算结果比对。退出码 0=全部核验通过，1=存在失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保从任意当前目录双击/调用都能导入同目录的 handoff_review.py。
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import handoff_review as hr  # noqa: E402


def main(argv: list[str]) -> int:
    paths = argv
    if not paths:
        paths = [str(hr.DEFAULT_WITNESS_DIR / hr._w1_name()),
                 str(hr.DEFAULT_WITNESS_DIR / hr._w2_name())]

    rc = 0
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            print(f"[FAIL] 见证文件不存在: {p}")
            rc = 1
            continue
        res = hr.verify_witness_file(p)
        status = "PASS" if res["ok"] else "FAIL"
        print(f"[{status}] {res['path']}")
        print(f"        kind={res['kind']}  sha256={res['file_sha256']}")
        for c in res["checks"]:
            mark = "[ok] " if c["pass"] else "[XX] "
            line = "  " + mark + c["check"]
            if c["detail"]:
                line += f"  -> {c['detail']}"
            print(line)
        if not res["ok"]:
            rc = 1
    print("-" * 68)
    print("独立核验结果：", "ALL PASS" if rc == 0 else "FAILED")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
