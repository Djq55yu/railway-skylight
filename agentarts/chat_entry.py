# -*- coding: utf-8 -*-
"""chat_entry.py —— mode=CHAT：把一句人话翻译成一次带参调用

两轮工具调用（宿主执行工具，模型只发请求）：

    round 1  {text}                    → {status:"tool_call", tool_calls:[...]}
    round 2  {text, tool_results}      → {status:"ok"|"need_choice"|"not_found", command, echo}

三条硬约束：
  1. 模型只能申请白名单里的工具，只能从候选里选 windowId（不许自己造）；
  2. 最多两轮，round ≥ 2 再申请工具直接判 error；
  3. 模型不可用（没 key / RENDER_MODE=template / 调用失败）时退回规则：
     日期正则 + 候选唯一即选中，保证演示不依赖网络。

它不判定、不查库、不落盘 —— 只产出「该调哪一次、带什么参数」。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

MODEL_TOOL = "list_windows"
TOOL_WHITELIST = {"list_windows", "get_window_tasks", "get_clause"}
MAX_ROUND = 2
DEFAULT_TASK_MODE = "PRE_REVIEW"

DATE_FULL = re.compile(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")
DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[月/]\s*(\d{1,2})(?!\d)")
TIME_HM = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")


# ---------------------------------------------------------------- 基础
def _llm_enabled() -> bool:
    mode = os.getenv("RENDER_MODE", "auto").strip().lower()
    return mode != "template" and bool(os.getenv("MODEL_API_KEY", "").strip())


def _call_model(system: str, user: str, max_tokens: int = 800) -> str | None:
    if not _llm_enabled():
        return None
    try:
        import httpx

        api_key = os.getenv("MODEL_API_KEY", "").strip()
        model = os.getenv("MODEL_NAME", "deepseek-v4.1-flash")
        base = os.getenv("MODEL_URL", "https://api.modelarts-maas.com/openai/v1").rstrip("/")
        with httpx.Client(timeout=30) as cli:
            resp = cli.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}],
                      "temperature": 0.0, "max_tokens": max_tokens},
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"].get("content") or "").strip() or None
    except Exception:
        return None


def _load_json(text: str | None) -> dict | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_date(text: str) -> dict:
    """从一句话里抽日期。带年份就给 date，只有月日就给 month/day。"""
    m = DATE_FULL.search(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return {"date": f"{y:04d}-{mo:02d}-{d:02d}"}
    m = DATE_MD.search(text)
    if m:
        return {"month": int(m.group(1)), "day": int(m.group(2))}
    return {}


def parse_time(text: str) -> str:
    m = TIME_HM.search(text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _echo(win: dict) -> str:
    date = win.get("date") or ""
    start = win.get("start") or ""
    end = win.get("end") or ""
    n = win.get("taskCount")
    tail = f"，{n} 条作业" if n not in (None, "") else ""
    return f"{date} {start}—{end}{tail}".strip()


# ---------------------------------------------------------------- round 1
PLAN_SYSTEM = """你是铁路天窗核查系统的意图解析器。只输出一个 JSON 对象，不要解释、不要代码块。

可选工具只有一个：list_windows
  参数：{"date":"YYYY-MM-DD"} 或 {"month":9,"day":23}（不知道年份时用后者）
  作用：按日期查候选天窗。

输出格式（二选一）：
  {"action":"tool_call","tool":"list_windows","args":{...}}
  {"action":"reject","reason":"<为什么无法理解，20 字以内>"}

规则：
1. 用户提到日期就填进 args；没提日期就输出 {"args":{}}（表示取最近的几个天窗）。
2. 用户问的是条文、闲聊、或与天窗核查无关时，用 reject。
3. 不要输出 windowId —— 你还不知道有哪些候选。"""


def round1(req: dict) -> dict:
    text = str(req.get("text") or "").strip()
    if not text:
        return {"status": "error", "reply": "没有收到问题内容。"}

    plan = _load_json(_call_model(PLAN_SYSTEM, f"今天是 2026-11-23。用户说：「{text}」"))
    if plan and plan.get("action") == "reject":
        return {"status": "not_found",
                "reply": f"没听懂要查哪个天窗：{plan.get('reason') or '请换一种说法'}。"
                         f"可以直接说「核查 9 月 23 日那个天窗」。"}
    if plan and plan.get("action") == "tool_call" and plan.get("tool") in TOOL_WHITELIST:
        args = plan.get("args") or {}
        if isinstance(args, dict):
            return _tool_call(args)

    # 规则兜底：抽日期；抽不到就让宿主给最近的几个
    args = parse_date(text)
    if not args:
        return {"status": "not_found",
                "reply": "没听出日期。可以直接说「核查 9 月 23 日那个天窗」。"}
    return _tool_call(args)


def _tool_call(args: dict) -> dict:
    return {"status": "tool_call",
            "tool_calls": [{"name": MODEL_TOOL, "args": args}]}


# ---------------------------------------------------------------- round 2
SELECT_SYSTEM = """你是铁路天窗核查系统的选择器。只输出一个 JSON 对象，不要解释、不要代码块。

给你用户的原始问题和若干候选天窗（每个含 windowId / date / start / end / taskCount），
选出唯一匹配的那个。

输出格式（三选一）：
  {"action":"select","windowId":9}
  {"action":"ambiguous"}
  {"action":"none"}

规则：
1. 只能从候选里挑，不许新造 windowId。
2. 用户没说时段、但同一天有多个候选时，输出 ambiguous。
3. 候选全部对不上日期时，输出 none。"""


def round2(req: dict) -> dict:
    text = str(req.get("text") or "").strip()
    candidates = _candidates(req)
    if not candidates:
        return {"status": "not_found", "reply": "没有查到符合条件的候选天窗。"}

    picked = None
    plan = _load_json(_call_model(
        SELECT_SYSTEM,
        f"用户说：「{text}」\n候选：" + json.dumps(candidates, ensure_ascii=False)))
    if plan:
        if plan.get("action") == "ambiguous":
            return _need_choice(candidates)
        if plan.get("action") == "none":
            return {"status": "not_found", "reply": "候选里没有符合该日期的天窗。"}
        if plan.get("action") == "select":
            picked = _by_id(candidates, plan.get("windowId"))
            if picked is None:      # 模型造了不存在的 id → 当作没选
                return _need_choice(candidates)

    if picked is None:
        picked = _pick_by_rule(text, candidates)
    if picked is None:
        return _need_choice(candidates)

    return {
        "status": "ok",
        "command": {"windowId": picked.get("windowId"), "taskMode": DEFAULT_TASK_MODE},
        "echo": _echo(picked),
        "window": picked,
        "reply": f"我理解你要查 {_echo(picked)}，正在按编制预审跑。",
    }


def _candidates(req: dict) -> list[dict]:
    out: list[dict] = []
    for item in req.get("tool_results") or []:
        if not isinstance(item, dict) or item.get("name") != MODEL_TOOL:
            continue
        for win in item.get("result") or []:
            if isinstance(win, dict) and win.get("windowId") is not None:
                out.append(win)
    return out


def _by_id(candidates: list[dict], wid: Any) -> dict | None:
    try:
        want = int(wid)
    except (TypeError, ValueError):
        return None
    for c in candidates:
        try:
            if int(c.get("windowId")) == want:
                return c
        except (TypeError, ValueError):
            continue
    return None


def _pick_by_rule(text: str, candidates: list[dict]) -> dict | None:
    """规则兜底：时段精确命中 > 候选唯一。"""
    hm = parse_time(text)
    if hm:
        for c in candidates:
            if str(c.get("start") or "").startswith(hm):
                return c
    if len(candidates) == 1:
        return candidates[0]
    return None


def _need_choice(candidates: list[dict]) -> dict:
    return {
        "status": "need_choice",
        "candidates": [{"windowId": c.get("windowId"), "label": _echo(c)} for c in candidates],
        "reply": "同一天有多个天窗，请指定一个。",
    }


# ---------------------------------------------------------------- 入口
def handle(req: dict) -> dict:
    try:
        rnd = int(req.get("round") or 1)
    except (TypeError, ValueError):
        rnd = 1

    if rnd < 1:
        rnd = 1
    if rnd > MAX_ROUND:
        return {"status": "error", "round": rnd,
                "reply": f"超过工具轮次上限（{MAX_ROUND} 轮）。"}

    out = round1(req) if rnd == 1 else round2(req)
    out["mode"] = "CHAT"
    out["round"] = rnd
    return out
