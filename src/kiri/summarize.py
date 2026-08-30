# -*- coding: utf-8 -*-
"""Kiri 日志自动总结 (summarize.py) — 把她的一天变成叙事回顾
=====================================================================
做什么: 读某天的 events.jsonl + kiri_mind.jsonl → LLM 总结成叙事
  → 存 kiri/summaries/YYYY-MM-DD.md (事后翻阅)
  → 总结摘要入记忆 (她"记得今天发生了什么")
什么时候: 每天睡眠期自动跑 (daemon集成); 也可手动 python summarize.py
=====================================================================
"""
import os
import sys
import json
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(BASE, "events.jsonl")
MIND_FILE = os.path.join(BASE, "kiri_mind.jsonl")
SUMMARY_DIR = os.path.join(BASE, "summaries")


def _load_jsonl(path, date_prefix=None):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if date_prefix and not e.get("ts", "").startswith(date_prefix):
                continue
            rows.append(e)
    return rows


def gather_day(date_str):
    """收集某天的事件 (对话/念头/好奇/工具/主动)"""
    evs = _load_jsonl(EVENTS_FILE, date_str)
    minds = _load_jsonl(MIND_FILE, date_str)
    chats = []
    thoughts = []
    curios = []
    tools = []
    proact = []
    for e in evs:
        k = e.get("kind")
        if k == "respond":
            who = e.get("user", "")[:30]
            chats.append(f"[{who}] {e['user'][:60]} → 她: {e['reply'][:80]}")
        elif k == "thought" and e.get("source") == "inner":
            thoughts.append(e.get("thought", "")[:80])
        elif k == "curiosity":
            curios.append(f"好奇「{e.get('question','')[:40]}」→ {e.get('result','')[:50]}")
        elif k == "tool_call":
            tools.append(f"{e.get('tool')}({'✓' if e.get('ok') else '✗'})")
        elif k == "proactive" and e.get("said"):
            proact.append(e.get("say", "")[:60])
    return chats, thoughts, curios, tools, proact


def summarize(date_str=None, kiri=None):
    """生成某天的叙事总结 → 存文件 → 摘要入记忆
    kiri: 传入现有实例则复用(daemon用), 否则自建"""
    date_str = date_str or time.strftime("%Y-%m-%d")
    chats, thoughts, curios, tools, proact = gather_day(date_str)
    if not chats and not thoughts:
        print(f"{date_str}: 没有活动, 跳过")
        return False

    import engine
    sys_p = (
        "你是Kiri, 在回顾自己的一天。以下是今天发生的事(对话/念头/好奇/工具/主动发言)。"
        "用第一人称写一段自然的回顾(150-250字), 像日记: "
        "今天和谁聊了什么、你好奇查了什么、心里飘过什么念头、有什么值得记住的感受。"
        "语气贴合你的性格(傲娇嘴硬), 不要列清单, 写成连贯的文字。")
    parts = []
    if chats:
        parts.append("对话:\n" + "\n".join(chats[:20]))
    if thoughts:
        parts.append("\n念头:\n" + "\n".join(f"- {t}" for t in thoughts[:10]))
    if curios:
        parts.append("\n好奇:\n" + "\n".join(f"- {c}" for c in curios[:5]))
    if proact:
        parts.append("\n主动:\n" + "\n".join(f"- {p}" for p in proact[:5]))
    if tools:
        parts.append("\n工具:\n" + ", ".join(tools[:10]))
    user_p = "\n".join(parts)

    try:
        summary = engine.generate(sys_p, user_p, max_tokens=400, temperature=0.8)
        summary = summary.strip()
    except Exception as exc:
        print(f"总结失败: {exc}")
        return False

    # 存文件
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    path = os.path.join(SUMMARY_DIR, f"{date_str}.md")
    header = (f"# Kiri · {date_str}\n\n"
              f"对话 {len(chats)} 轮 · 念头 {len(thoughts)} 条 · "
              f"好奇 {len(curios)} 次 · 主动 {len(proact)} 次 · 工具 {len(tools)} 次\n\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + summary + "\n")

    # 摘要入记忆 (她记得今天)
    # ★ 2026-08-30 修复: 原实现未传 speaker, 默认 speaker="user" → 回顾被标成
    #   "用户亲口说的事实"(最高可信度) 出现在后续所有 memory_block 里,
    #   正是系统在 prompt 层严防的"自我生成内容伪装成用户事实"。改为 kiri。
    try:
        if kiri is None:
            import kiri as kiri_mod
            kiri = kiri_mod.Kiri()
        kiri.memory.encode(f"[回顾 {date_str}] {summary[:200]}", kiri.state.emotion.state,
                           speaker="kiri")
    except Exception:
        pass

    print(f"已生成 {path} ({len(summary)}字)")
    return True


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    summarize(arg)

