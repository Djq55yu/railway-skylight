# -*- coding: utf-8 -*-
"""test_chat.py —— mode=CHAT 的本地测试（强制走规则兜底，不依赖模型）

    python test_chat.py

覆盖：日期抽取、两轮循环、候选唯一/歧义、断网兜底、轮次上限、与 dispatch 的接线。
"""
from __future__ import annotations

import os
import sys

os.environ["RENDER_MODE"] = "template"      # 强制规则兜底，测试可重复

sys.stdout.reconfigure(encoding="utf-8")

import chat_entry  # noqa: E402
import dispatch  # noqa: E402

_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _failed
    if not cond:
        _failed += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


CANDS = [
    {"windowId": 9,  "date": "2026-09-23", "start": "07:30", "end": "09:20", "taskCount": 12},
    {"windowId": 10, "date": "2026-09-23", "start": "10:40", "end": "13:10", "taskCount": 5},
]


def r2(text: str, cands=CANDS, name="list_windows") -> dict:
    return chat_entry.handle({
        "mode": "CHAT", "round": 2, "text": text,
        "tool_results": [{"name": name, "result": cands}],
    })


def main() -> int:
    print("一、round 1：听懂要不要数据")
    out = chat_entry.handle({"mode": "CHAT", "round": 1, "text": "核查 9 月 23 号那个天窗"})
    check("抽不出年份也给 month/day", out["status"] == "tool_call"
          and out["tool_calls"][0]["args"] == {"month": 9, "day": 23},
          str(out.get("tool_calls")))
    out = chat_entry.handle({"mode": "CHAT", "round": 1, "text": "查一下 2026-09-23 的天窗"})
    check("带年份给 date", out["tool_calls"][0]["args"].get("date") == "2026-09-23",
          str(out.get("tool_calls")))
    out = chat_entry.handle({"mode": "CHAT", "round": 1, "text": "今天天气怎么样"})
    check("无关问题 → not_found", out["status"] == "not_found", out.get("reply", "")[:30])
    out = chat_entry.handle({"mode": "CHAT", "round": 1, "text": ""})
    check("空输入 → error", out["status"] == "error")

    print("\n二、round 2：从候选里选")
    out = r2("核查 9 月 23 号那个天窗", CANDS[:1])
    check("候选唯一 → ok", out["status"] == "ok")
    check("command 带 windowId 与 taskMode",
          out["command"] == {"windowId": 9, "taskMode": "PRE_REVIEW"}, str(out.get("command")))
    check("echo 含条数", "12 条" in out.get("echo", ""), out.get("echo"))
    out = r2("查一下 07:30 那个", CANDS)
    check("时段命中 → 选 9", out["status"] == "ok" and out["command"]["windowId"] == 9)
    out = r2("核查 9 月 23 号的天窗", CANDS)
    check("多个候选且没说时段 → need_choice",
          out["status"] == "need_choice" and len(out["candidates"]) == 2)
    out = r2("x", [], name="list_windows")
    check("无候选 → not_found", out["status"] == "not_found")
    out = r2("x", CANDS, name="delete_everything")
    check("非白名单工具的结果被忽略", out["status"] == "not_found")

    print("\n三、轮次上限")
    out = chat_entry.handle({"mode": "CHAT", "round": 3, "text": "x",
                             "tool_results": [{"name": "list_windows", "result": CANDS}]})
    check("round=3 → error", out["status"] == "error", out.get("reply", "")[:20])

    print("\n四、与 dispatch 接线")
    out = dispatch.handle_invocation({"mode": "chat", "text": "核查 9 月 23 号那个天窗"})
    check("小写 mode 也认", out.get("mode") == "CHAT", str(out.get("mode")))
    check("不需要 tasks", out.get("status") == "tool_call", str(out.get("status")))

    print("\n" + ("全部通过" if _failed == 0 else f"有 {_failed} 项失败"))
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
