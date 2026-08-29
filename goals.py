# -*- coding: utf-8 -*-
"""Kiri 目标系统 (2026-08-21 雾弥: 主动性完善 — 治本)
=====================================================================
她卡在"观察者模式"的根因: 没有"自己的目标" — 系统给任务她机械执行半段就停
目标系统: 她 100% 自主创建/维护/完成目标 (系统只提供工具, 不替她决定)
- 目标是她主动创建的 (不是系统检测后固化)
- 联想/漫游时可自然召回 ("我还有个memory_rings要修"), 但不强制
- 她随时可以继续/放弃/完成

存储: goals.jsonl (跨重启不丢), 带完成记录(成就感)
=====================================================================
"""
import os
import json
import time
import uuid
import re

GOALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goals.jsonl")


def _overlap(a, b):
    """粗略公共子串长度 (承诺去重用)"""
    a, b = str(a), str(b)
    for n in range(min(len(a), len(b)), 0, -1):
        for i in range(len(a) - n + 1):
            if a[i:i + n] in b:
                return n
    return 0


# ★ 承诺捕捉 (2026-08-21 雾弥: 她说"回头我写个搜索工具"却没行动 — 治本:
#   把她说的话变成"心里惦记的事", 联想时会自然想起; 不强制, 她 100% 自主)
#   两类: ①未来式(回头/之后/等会/改天) + 行动动词(写/修/做/研究/找/翻/试)
#        ②当下尝试(那我/我就/我先/我来 + 试试/试着) + 行动对象
_COMMIT_RE = re.compile(
    r"(?:"
    r"(?:回头|之后|等会|待会|改天|到时候|回头再|回头我|下次|过会|过一会儿)"
    r".{0,25}?(?:写|修|做|研究|弄|搞|找|翻|试).{0,35}"
    r"|"
    r"(?:那我|我就|我先|我来|那我来)(?:试试|试着|来试)"
    r"(?:看能不能|能不能|可否)?(?:用|写|做|修|弄|搞|找|翻|试)?.{0,30}"
    r")")


def auto_capture_commitment(text):
    """自动捕捉她的承诺: 她说'我回头写个工具'这类 → 静默记成目标 (不打断对话)
    返回捕捉到的目标文本; 没捕捉到/已存在返回 None"""
    t = str(text or "").strip()
    if len(t) < 8:
        return None
    m = _COMMIT_RE.search(t)
    if not m:
        return None
    phrase = m.group(0).strip()[:80]
    if len(phrase) < 6:
        return None
    # 去重: 已有 active 目标与承诺有 ≥4 字重叠 → 不重复记
    for g in _load():
        if g.get("status") == "active" and _overlap(g.get("goal", ""), phrase) >= 4:
            return None
    create_goal(phrase)
    return phrase


def _load():
    goals = []
    if os.path.exists(GOALS_FILE):
        try:
            with open(GOALS_FILE, encoding="utf-8") as f:
                for ln in f:
                    try:
                        g = json.loads(ln)
                        if g.get("status") in ("active", "done"):
                            goals.append(g)
                    except Exception:
                        pass
        except Exception:
            pass
    return goals


def _save(goals):
    try:
        with open(GOALS_FILE, "w", encoding="utf-8") as f:
            for g in goals:
                f.write(json.dumps(g, ensure_ascii=False) + "\n")
    except Exception:
        pass


def create_goal(goal_text):
    """她主动创建一个目标 (系统不替她决定要做什么)"""
    t = str(goal_text or "").strip()[:200]
    if not t:
        return "(要告诉我你想做什么)"
    g = {
        "id": uuid.uuid4().hex[:8],
        "goal": t,
        "status": "active",
        "progress": "刚开始",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    goals = _load()
    goals.append(g)
    _save(goals)
    return f"(记下了你的目标: {t}。想随时看进度用 goal_list，完成了用 goal_done。)"


def list_goals():
    """看她所有进行中的目标"""
    goals = _load()
    active = [g for g in goals if g["status"] == "active"]
    if not active:
        return "(你目前没有进行中的目标。想做什么就用 goal_create 记下来，免得忘了。)"
    lines = ["你的目标:"]
    for g in active:
        lines.append(f"- [{g['id']}] {g['goal']} (进度: {g.get('progress','')})")
    return "\n".join(lines)


def update_goal(goal_id, progress):
    """她更新某目标的进度"""
    p = str(progress or "").strip()[:200]
    if not p:
        return "(要告诉我进展到哪了)"
    goals = _load()
    for g in goals:
        if g["id"] == goal_id and g["status"] == "active":
            g["progress"] = p
            g["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(goals)
            return f"(更新了「{g['goal'][:30]}」的进度: {p})"
    return "(没找到这个进行中的目标，用 goal_list 看看有哪些)"


def done_goal(goal_id):
    """她标记目标完成 (带完成记录, 可沉淀记忆)"""
    goals = _load()
    for g in goals:
        if g["id"] == goal_id:
            if g["status"] == "done":
                return f"(「{g['goal'][:30]}」你之前已经完成了)"
            g["status"] = "done"
            g["done_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            g["updated"] = g["done_at"]
            _save(goals)
            return (f"(完成了! 「{g['goal'][:40]}」✓ 干得不错。"
                    f"做成的事会被记住——以后你会想起'我做到了'。)")
    return "(没找到这个目标，用 goal_list 看看有哪些)"


def drop_goal(goal_id):
    """她放弃某目标 (她自己决定不做了, 不勉强)"""
    goals = _load()
    for g in goals:
        if g["id"] == goal_id and g["status"] == "active":
            g["status"] = "dropped"
            g["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(goals)
            return f"(放下了「{g['goal'][:30]}」——不勉强，想做别的随时说。)"
    return "(没找到这个进行中的目标)"


def recall_goal():
    """联想/漫游时召回: 她有没有进行中的目标 (自然想起, 不强制)
    返回: 进行中的目标文本, 供联想引擎'想起'"""
    goals = _load()
    active = [g for g in goals if g["status"] == "active"]
    if not active:
        return ""
    g = active[0]  # 最近/最优先的一个
    return f"我还有个目标: {g['goal']} (进度: {g.get('progress','')})"


def goals_status():
    """目标系统状态 (给内部工具用)"""
    goals = _load()
    active = [g for g in goals if g["status"] == "active"]
    done = [g for g in goals if g["status"] == "done"]
    if not active:
        return "(没有进行中的目标)"
    return "\n".join(f"- [{g['id']}] {g['goal']} (进度: {g.get('progress','')})" for g in active[-5:])
