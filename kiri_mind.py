# -*- coding: utf-8 -*-
"""Kiri 心路日志 (kiri_mind.jsonl) — 她的内心发生的事, 事后翻阅为主
=====================================================================
只记录三类事件, 按时间顺序:
  [对话] 你说/她说 (完整对话, 双向)
  [联想] 联想念头 (所有念头, 含salience/来源记忆)
  [好奇] 好奇问题 + 搜索结果
  [工具] MCP工具调用 (工具/参数/结果/成败/耗时)
=====================================================================
"""
import os
import json
import time

MIND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiri_mind.jsonl")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write(kind, **data):
    try:
        ev = {"ts": _now(), "kind": kind, **data}
        with open(MIND_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---- 对话 ----
def chat_user(text, user=None):
    _write("chat_user", text=str(text)[:300], user=str(user or "雾弥")[:20])

def chat_kiri(text, user=None):
    _write("chat_kiri", text=str(text)[:300], user=str(user or "雾弥")[:20])

def proactive(say, reason):
    _write("proactive", say=str(say)[:300], reason=str(reason)[:30])

# ---- 联想 ----
def thought(text, salience, memory=None, user=None):
    _write("thought", text=str(text)[:200], salience=round(float(salience), 3),
           memory=str(memory)[:80] if memory else "",
           user=str(user or "雾弥")[:20])

def curiosity(question, keywords="", result=""):
    _write("curiosity", question=str(question)[:100], keywords=str(keywords)[:50],
           result=str(result)[:250])

def heard(text, decision, user=None, scene="group"):
    """群里听到了但没回: 她潜水时听到的话 + 为什么没插话 (静默留痕)"""
    _write("heard", text=str(text)[:200], decision=str(decision)[:40],
           user=str(user or "")[:20], scene=scene)

# ---- 工具 ----
def tool_call(tool, args, result, ok, latency):
    _write("tool_call", tool=str(tool)[:30], args=str(args)[:80],
           result=str(result)[:250], ok=bool(ok), latency=round(float(latency), 2))


def read(n=200):
    """读最近 n 条 (供翻阅/监控)"""
    rows = []
    try:
        if not os.path.exists(MIND_FILE):
            return rows
        with open(MIND_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return rows[-n:]


def recent_thoughts(n=6):
    """最近 n 条联想念头 [(text, salience)] — 连续意识流: 走神接续用
    ★ 2026-08-21 雾弥: "内部思维流本质上还是离散的" — 念头要跨周期/跨进程接续"""
    rows = []
    try:
        if not os.path.exists(MIND_FILE):
            return rows
        with open(MIND_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("kind") == "thought" and r.get("text"):
                        rows.append((str(r["text"]), float(r.get("salience", 0) or 0)))
                except Exception:
                    pass
    except OSError:
        pass
    return rows[-n:]
