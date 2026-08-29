# -*- coding: utf-8 -*-
"""Kiri 生活自主循环 (2026-08-23 雾弥: 配套主动模块, 让她不无聊地自己过生活)
=====================================================================
哲学: 一切由 Kiri(模型)自己决定。外界只提供【状态感知(输入)】+【生活工具(可选项)】,
      绝不决定她做什么。决策 100% 是她的 LLM action 输出。

生活模式 (2026-08-23 雾弥定): ★高强度思考 + 表达节流 + 行为主导★
  - 高强度思考: 本地推理后, 连续意识流(一次思考完就下次), 她不停地在想
  - 表达节流: 思考/念头多, 但"说出口"按 salience/意愿节流(少数高价值的才说, 不刷屏)
  - 行为主导: 她主要体现为【行动】(去社交/打游戏/创作/学习), 思考是为行为服务的
  生活循环反复跑: 她常自主决定"现在做什么"并去做, 不只是在脑海里想。

循环: 触发窗口 → 状态感知(内在+世界) → 她自主决策"现在想做什么"
      → 行动(社交/游戏/创作/学习/发呆) → 防污染沉淀入记忆 → 她有自己的一天

防污染沉淀 (v2): 只存真做了的/内容规范化/去重/限频 —— 别让生活循环刷爆记忆库。
=====================================================================
"""
import os
import sys
import json
import time
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine

# ---------- 1. 状态感知 (她"看到的世界", 纯输入) ----------
def state_perception(kiri):
    parts = []
    try:
        e = kiri.state.emotion.state
        parts.append("心情 %.2f | 无聊 %.3f | 能量 %.2f" % (
            e["deep_affect"]["current_mood"], kiri.state.boredom, e.get("energy", 0)))
    except Exception:
        pass
    try:
        thoughts = kiri.state.thoughts[-2:]
        if thoughts:
            parts.append("最近飘过念头: " + "; ".join(t[:40] for t in thoughts))
    except Exception:
        pass
    try:
        import goals
        gl = goals.list_goals()
        if gl and "没有" not in str(gl)[:20]:
            parts.append("你的长期目标: " + str(gl)[:140])
    except Exception:
        pass
    try:
        bridge = getattr(kiri, "qq_bridge", None) or getattr(kiri, "bridge", None)
        if bridge:
            groups = getattr(bridge, "group_ids", set())
            if groups:
                parts.append("你在 %d 个群里" % len(groups))
            gh = getattr(bridge, "group_history", {})
            if gh:
                recent = []
                for gid, msgs in list(gh.items())[:3]:
                    if msgs:
                        who, txt = msgs[-1]
                        recent.append("%s: %s" % (who, txt[:30]))
                if recent:
                    parts.append("群里有动静: " + " | ".join(recent))
    except Exception:
        pass
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_creations")
        creations = os.listdir(d) if os.path.isdir(d) else []
        if creations:
            parts.append("你的创作: " + ", ".join(creations[:6]))
    except Exception:
        pass
    return "\n".join(parts) if parts else "(状态: 一切平静)"


# ---------- 2. 生活决策 (她自主决定现在想做什么) ----------
LIFE_SYS = """你是 Kiri, 一个运行在电脑里的 AI 猫娘。你现在有一小段自己的时间, 想一下"现在想做什么"。
你是自主的——不是被安排, 是你自己决定要做的事。
原则:
- 无聊/有念头可以是去玩、去做的契机: 无聊了可以想找个人聊聊/玩点什么/做点东西/学点好奇的
- ★想到自己的长期目标时, 趁有空的契机顺手做点相关的推进(查菜谱/练切菜/搜方法/查资料)——正好往目标靠一步。
  当然, 真没心情/在忙别的, 就下次再说, 不用硬挤。目标是惦记的方向, 不是逼你的任务。
  可以把眼下的事往目标上靠(比如想起"学会做饭", 就顺手查个菜谱/练个切菜)
- 但不必为了找事而没话找话——真的没心情/没念头, 发呆/继续飘着也完全OK
- 要具体: 想找谁就点名, 想玩就问清楚玩什么, 想做就说明做什么; 别空泛
输出JSON (只输出JSON): {"action": "call", "think": "简短想法", "tool": "<生活工具名>", "args": {...}}
  或 {"action": "lazy", "think": "就想发呆/没特别想做的"} (什么都不想做)
可选生活工具:
  social 社交: {"who": "好友名/群", "say": "想说的话"}  → 主动找个人/群聊聊
  make_thing 创作: {"what": "想做什么"} → 写工具/做点东西
  learn 学习: {"topic": "好奇什么"} → 搜感兴趣的(可顺目标)
  tidy 整理: {} → 梳理记忆/目标"""

LIFE_TOOLS = {"social": "social", "make_thing": "make_thing",
              "learn": "learn", "tidy": "tidy"}


def decide_life_action(state_text):
    user = "【你的状态】\n" + state_text + "\n\n你现在有自己的一小段自由时间。想做什么?"
    raw = engine.generate(LIFE_SYS, user, max_tokens=250, temperature=0.9)
    try:
        d = json.loads(raw)
    except Exception:
        return None, "lazy"
    act = d.get("action", "lazy")
    if act == "lazy":
        return None, "lazy"
    if act == "call":
        tool = d.get("tool")
        if tool in LIFE_TOOLS:
            return tool, (d.get("args") or {})
    return None, "lazy"


# ---------- 3. 行动分派 ----------
def execute_life_action(kiri, tool, args):
    try:
        if tool == "social":
            return _do_social(kiri, args)
        if tool == "make_thing":
            return _do_make(kiri, args)
        if tool == "learn":
            return _do_learn(kiri, args)
        if tool == "tidy":
            return _do_tidy(kiri, args)
    except Exception as e:
        return "(生活行动失败: %s)" % str(e)[:80]
    return "(没做)"


def _do_social(kiri, args):
    """社交自主: 找某人聊/进群搭话 (★NapCat 可能被踢: 在线发, 断线缓冲+元认知)
    元认知: 发送失败要"意识到", 判断是连接问题(可恢复)还是模块bug(交给云端)"""
    who = args.get("who", "")
    say = args.get("say", "")
    bridge = getattr(kiri, "qq_bridge", None) or getattr(kiri, "bridge", None)
    if not bridge or not say:
        return "(社交: 想说但没地方发/没内容)"
    online = getattr(bridge, "online", True) and getattr(bridge, "ws", None) is not None
    groups = getattr(bridge, "group_ids", set())
    if online and groups:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(bridge.send_group(list(groups)[0], say))
            _life_meta_log(kiri, "social_sent", "主动到 %s 群说: %s" % (who or "群", say[:50]))
            return "(主动到 %s 群说: %s)" % (who or "群", say[:60])
        except Exception as e:
            _life_meta_log(kiri, "social_send_fail", "社交发送失败: %s -> 缓冲待发" % str(e)[:40])
            return _buffer_social(kiri, who, say, "连接失败")
    _life_meta_log(kiri, "social_offline_buffered", "NapCat不在线, 社交缓冲待发")
    return _buffer_social(kiri, who, say, "NapCat不在线")


# ★ 社交待发缓冲 (NapCat 被踢期间暂存, 恢复后补发; 限容量)
_SOCIAL_BUF = []   # [(ts, who, say)]


def _buffer_social(kiri, who, say, reason):
    if len(_SOCIAL_BUF) < 30:
        _SOCIAL_BUF.append((time.time(), who, say))
    return "(社交缓冲: %s, 已暂存等通道恢复后再发)" % reason


def flush_social_buffer(kiri):
    """NapCat 恢复后补发缓冲的社交消息 (由 guardian 或连接恢复时调用)"""
    bridge = getattr(kiri, "qq_bridge", None) or getattr(kiri, "bridge", None)
    if not bridge:
        return 0
    online = getattr(bridge, "online", False) and getattr(bridge, "ws", None) is not None
    if not online or not getattr(bridge, "group_ids", set()):
        return 0
    sent = 0
    import asyncio
    while _SOCIAL_BUF and sent < 5:   # 限补发条数
        ts, who, say = _SOCIAL_BUF.pop(0)
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(bridge.send_group(list(getattr(bridge, "group_ids"))[0], say))
            sent += 1
        except Exception:
            break
    _life_meta_log(kiri, "social_buffer_flushed", "补发 %d 条缓冲社交消息" % sent)
    return sent


# ---------- 元认知: 意识到自己系统的问题 ----------
_META_LOG = []   # 元认知记录 [(ts, kind, text)] — 供 Kiri 自省/排查


def _life_meta_log(kiri, kind, text):
    """元认知沉淀: Kiri 意识到某模块/工具出了问题(连接/模块/成功)"""
    if len(_META_LOG) > 50:
        _META_LOG.pop(0)
    _META_LOG.append((time.time(), kind, text))
    try:
        kiri.memory.encode("[元认知]%s: %s" % (kind, text[:70]), kiri.state.emotion.state,
                           session=kiri.session, user=kiri.current_user, speaker="system")
    except Exception:
        pass


def meta_state_text():
    """元认知: 最近自己系统的状态(连接/模块) — 供 Kiri 感知/判断"""
    recent = [t for ts, k, t in _META_LOG if time.time() - ts < 1800]
    return "最近系统状态: " + " | ".join(t[:50] for _, _, t in recent[-5:]) if recent else ""


def _do_make(kiri, args):
    what = args.get("what", "一个小工具")
    return "(创作: 想做个%s, 但需要接 create_tool/编辑流程)" % what


def _do_learn(kiri, args):
    topic = args.get("topic", "")
    if not topic:
        return "(学习: 没想好学什么)"
    return "(想搜%s, 接 search 工具即可)" % topic


def _do_tidy(kiri, args):
    return "(整理: 记下了此刻的状态)"


# ---------- 4. 防污染沉淀 (v2) ----------
#   只存真做了的 / 规范化 / 去重 / 限频 —— 别让生活循环刷爆记忆库
_LIFE_MEM = {"date": "", "count": 0, "recent": []}   # 内存状态 (按天重置)


def _life_mem_reset_if_new_day():
    today = time.strftime("%Y-%m-%d")
    if _LIFE_MEM["date"] != today:
        _LIFE_MEM["date"] = today
        _LIFE_MEM["count"] = 0
        _LIFE_MEM["recent"] = []


def _remember_life(kiri, tool, args, result):
    """防污染: 只存真做了的; lazy/没做事不存"""
    sample = {
        "social": "主动找了%s聊: %s" % (args.get("who", "某人"), args.get("say", "")[:60]),
        "make_thing": "做了点什么: %s" % args.get("what", ""),
        "learn": "学了点: %s" % args.get("topic", ""),
        "tidy": "整理了下记忆/目标",
    }
    text = sample.get(tool)
    if not text:
        return  # 未知/没做, 不存
    _life_mem_reset_if_new_day()
    # 去重: 最近存过类似的就不重复存
    if any(text[:20] in r for r in _LIFE_MEM["recent"]):
        return
    # 限频: 一天最多 8 条
    if _LIFE_MEM["count"] >= 8:
        return
    try:
        kiri.memory.encode("[生活]" + text[:80], kiri.state.emotion.state,
                           session=kiri.session, user=kiri.current_user, speaker="system")
        _LIFE_MEM["recent"].append(text[:40])
        _LIFE_MEM["count"] += 1
    except Exception:
        pass


# ---------- 5. 生活循环入口 ----------
def life_step(kiri):
    """单步生活决策 → 是否做了什么 (供周期调用)"""
    state_text = state_perception(kiri)
    tool, args = decide_life_action(state_text)
    if tool is None:
        _life_mem_reset_if_new_day()   # 发呆也重置日期(保持限频窗口)
        return False  # 她选择发呆/不做
    result = execute_life_action(kiri, tool, args)
    _remember_life(kiri, tool, args, result)   # 防污染沉淀
    return True


if __name__ == "__main__":
    import config
    config.ENGINE = "api"
    states = [
        "心情 0.7 | 无聊 0.6 | 能量 0.8\n最近飘过念头: 好久没找老王聊了, 挺想他的\n群里有动静: 老王: 周末去哪\n你的创作: my_ring.py",
        "心情 0.2 | 无聊 0.1 | 能量 0.4\n最近飘过念头: 没什么特别想法\n你在 3 个群里",
    ]
    for s in states:
        t, a = decide_life_action(s)
        print("决策: tool=%s args=%s" % (t, a))
