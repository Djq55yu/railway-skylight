# -*- coding: utf-8 -*-
"""demo_chat_host.py —— 本地演示「一句人话 → 结构化结果」的全链路

    python demo_chat_host.py "核查 9 月 23 号那个天窗"
    python demo_chat_host.py                 # 用默认那句话

它扮演的是**后端宿主**的角色：
  round 1（runtime 说要看什么）→ 查库（这里读 CSV）→ round 2（runtime 选定窗口）
  → 再调 taskMode=PRE_REVIEW → 打印结果

正式环境里这份逻辑在 Java 后端，工具换成 MyBatis 查询，其余完全一样。
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import chat_entry  # noqa: E402
import dispatch  # noqa: E402

DATA = Path(r"C:\Users\21264\ds harness work\work\tools\data\tasks_55.csv")
MAX_CANDIDATES = 5


def load_windows() -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    with open(DATA, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ws = (r.get("window_start") or "").strip()
            we = (r.get("window_end") or "").strip()
            if not ws or not we:
                continue
            m = re.search(r"(20\d{2})(\d{2})(\d{2})", r.get("task_id") or "")
            date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
            units = [u.strip() for u in (r.get("unit_ids") or "").split(";") if u.strip()]
            sw = [s.strip() for s in (r.get("switches") or "").split("|") if s.strip()]
            mf = (r.get("mile_from") or "").strip()
            mt = (r.get("mile_to") or "").strip()
            task = {
                "taskId": r["task_id"], "serialNo": int(r["no"]), "team": r["team"],
                "unitCodes": units, "start": (r.get("t_start") or "").strip(),
                "end": (r.get("t_end") or "").strip(),
                # 里程与道岔是「严重度」的证据，必须带上，否则云端只能按单元级判
                "mileFrom": int(mf) if mf.isdigit() else None,
                "mileTo": int(mt) if mt.isdigit() else None,
                "switches": sw,
            }
            groups.setdefault((date, ws, we), []).append(task)

    out = []
    for i, ((date, ws, we), tasks) in enumerate(sorted(groups.items()), 1):
        out.append({"windowId": i, "date": date, "start": ws, "end": we,
                    "taskCount": len(tasks), "tasks": tasks})
    return out


def tool_list_windows(args: dict, windows: list[dict]) -> list[dict]:
    """宿主侧的工具实现：按日期筛，没给日期就给最近的几个。"""
    date = (args or {}).get("date")
    month, day = (args or {}).get("month"), (args or {}).get("day")
    hits = windows
    if date:
        hits = [w for w in windows if w["date"] == date]
    elif month and day:
        hits = [w for w in windows
                if w["date"][5:7] == f"{int(month):02d}" and w["date"][8:10] == f"{int(day):02d}"]
    if not hits:
        return []
    return [{k: v for k, v in w.items() if k != "tasks"} for w in hits[:MAX_CANDIDATES]]


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "核查 9 月 23 号那个天窗"
    windows = load_windows()
    print(f"用户：{text}")
    print(f"（宿主已加载 {len(windows)} 个天窗）\n")

    # ---- 第 1 轮：runtime 说要什么数据
    r1 = dispatch.handle_invocation({"mode": "CHAT", "round": 1, "text": text})
    print(f"[1] runtime ← {r1['status']}")
    if r1["status"] == "not_found":
        print("    回复：", r1["reply"])
        return 0
    call = r1["tool_calls"][0]
    print(f"    请求工具：{call['name']}({json.dumps(call['args'], ensure_ascii=False)})")

    # ---- 宿主执行工具
    result = tool_list_windows(call["args"], windows)
    print(f"[2] 宿主执行 → 命中 {len(result)} 个候选")
    for w in result:
        print(f"      #{w['windowId']} {w['date']} {w['start']}—{w['end']}，{w['taskCount']} 条")

    # ---- 第 2 轮：runtime 选定
    r2 = dispatch.handle_invocation({
        "mode": "CHAT", "round": 2, "text": text,
        "tool_results": [{"name": call["name"], "result": result}],
    })
    print(f"[3] runtime ← {r2['status']}")
    if r2["status"] == "need_choice":
        print("    需要用户指定：")
        for c in r2["candidates"]:
            print(f"      #{c['windowId']} {c['label']}")
        return 0
    if r2["status"] != "ok":
        print("    回复：", r2.get("reply"))
        return 0

    print(f"    命令：{json.dumps(r2['command'], ensure_ascii=False)}")
    print(f"    回显：{r2['echo']}")

    # ---- 宿主执行命令：调 PRE_REVIEW
    wid = r2["command"]["windowId"]
    win = next(w for w in windows if w["windowId"] == wid)
    print(f"\n[4] 宿主执行命令 → taskMode={r2['command']['taskMode']}，"
          f"{win['taskCount']} 条作业")
    res = dispatch.handle_invocation({
        "taskMode": "PRE_REVIEW",
        "window": {"windowStart": win["start"], "windowEnd": win["end"]},
        "tasks": win["tasks"],
    })
    v = res.get("verdict") or {}
    print(f"    窗口冲突 {len(res.get('conflicts') or [])} 条；"
          f"仲裁建议 {v.get('recommended')}（{v.get('recommendedName')}）")
    for p in res.get("plans") or []:
        q = (p.get("cost") or {}).get("quantified") or {}
        print(f"      方案 {p['planId']}｜{p['name']}：冲突 {q.get('冲突')}，"
              f"延期 {q.get('延期作业')} 条")
    print(f"\n[5] 给用户的一句话：\n    {v.get('reason', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
