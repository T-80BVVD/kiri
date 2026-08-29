# -*- coding: utf-8 -*-
"""Kiri 数据日志 (data_log.jsonl) — 做数据集和深度分析用的数据, 与心路日志分开
=====================================================================
为什么单独开: kiri_mind.jsonl 是"事后翻阅"的心路; 数据集需要的是
规整、完整、带元数据的样本。这里实时落盘, dataset.py 直接读它,
不用事后从 events 重建、不用时间窗口猜内心独白。

记录的样本 (每行一个JSON):
  respond:  完整对话样本 {sender, scene, input, output, mood, pleasure,
            boredom, latency, memories, tools, thought(导出时按session关联)}
  thought:  内心独白 {text, salience, session, user} — 与respond同session关联
  tool_call: 工具调用 {tool, args, result, ok, latency}
  wander:   漫游 {zone, label, result}
  curiosity: 好奇 {question, result}
  proactive: 主动发言 {say, reason}
=====================================================================
"""
import os
import json
import time

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_log.jsonl")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write(kind, **data):
    try:
        ev = {"ts": _now(), "kind": kind, **data}
        with open(DATA_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---- 完整对话样本 (数据集核心) ----
def respond(sender, scene, input_text, output, mood=None, pleasure=None,
            boredom=None, latency=None, memories=None, tools=None, error=None):
    _write("respond",
           sender=str(sender or "雾弥")[:30],
           scene=str(scene or "private"),
           input=str(input_text)[:300],
           output=str(output)[:300],
           mood=mood, pleasure=pleasure, boredom=boredom, latency=latency,
           memories=[str(m)[:80] for m in (memories or [])],
           tools=[str(t)[:60] for t in (tools or [])],
           error=error)


# ---- 内心独白 (与respond同session, 导出时关联) ----
def thought(text, salience, session="", user=None):
    _write("thought", text=str(text)[:200], salience=round(float(salience or 0), 3),
           session=str(session)[:12], user=str(user or "")[:20])


# ---- 工具调用 ----
def tool_call(tool, args, result, ok, latency):
    _write("tool_call", tool=str(tool)[:30], args=str(args)[:80],
           result=str(result)[:250], ok=bool(ok), latency=round(float(latency or 0), 2))


# ---- 漫游 (她刷了什么, 兴趣分析) ----
def wander(zone, label, result):
    _write("wander", zone=str(zone or "")[:20], label=str(label)[:40], result=str(result)[:250])


# ---- 好奇 ----
def curiosity(question, result):
    _write("curiosity", question=str(question)[:100], result=str(result)[:250])


# ---- 主动发言 ----
def proactive(say, reason):
    _write("proactive", say=str(say)[:200], reason=str(reason)[:40])


# ---- 读取 (供 dataset.py) ----
def read(n=5000, kinds=None):
    rows = []
    try:
        if not os.path.exists(DATA_FILE):
            return rows
        with open(DATA_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kinds and e.get("kind") not in kinds:
                    continue
                rows.append(e)
    except OSError:
        pass
    return rows[-n:]
