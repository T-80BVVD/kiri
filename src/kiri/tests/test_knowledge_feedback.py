# -*- coding: utf-8 -*-
"""test_knowledge_feedback.py — 知识页反思闭环测试 (NEKO reflection吸收)
场景: A 用户确认→confirmed B 两次确认→promoted C 单次否定→负证据保留
      D 两次否定→删除 E 忽略→衰减 F 浮出标注 (pending 带"还不太确定")
不调 LLM (直接手工 add 画像条目), 用独立测试集合, 测完删除
"""
import sys
import os
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory as memory_mod
import memory_knowledge as mk

TEST_USER = "_p7test"


def setup():
    m = memory_mod.Memory()
    kb = mk.KnowledgeBase(m)
    col = kb._col(TEST_USER)
    # 清理上次残留 (chroma 持久化; 清条目不删集合, 避免缓存失效)
    try:
        data = col.get(limit=col.count() or 1)
        if data and data.get("ids"):
            col.delete(ids=data["ids"])
    except Exception:
        pass
    for tid, txt in [("t1", "雾弥喜欢黑色"), ("t2", "雾弥在学吉他"), ("t3", "雾弥累时会找我")]:
        col.add(ids=[tid], documents=[txt],
                metadatas=[{"user": TEST_USER, "status": mk.STATUS_PENDING,
                            "evidence": 0.0, "created": time.time(),
                            "updated": time.time(), "surfacings": 0}])
    return m, kb, col


def test_confirm_promote():
    m, kb, col = setup()
    # A: 用户确认 → confirmed
    c, d, i = kb.feedback(TEST_USER, "对，我确实喜欢黑色", ["t1"])
    assert (c, d) == (1, 0), (c, d)
    m1 = col.get(ids=["t1"])["metadatas"][0]
    assert m1["status"] == mk.STATUS_CONFIRMED and m1["evidence"] >= 1.0, m1
    print("A 确认升级 confirmed  OK")
    # B: 再次确认 → promoted
    kb.feedback(TEST_USER, "没错，就是黑色", ["t1"])
    m1 = col.get(ids=["t1"])["metadatas"][0]
    assert m1["status"] == mk.STATUS_PROMOTED, m1
    print("B 两次确认提升 promoted  OK")


def test_deny():
    m, kb, col = setup()
    # C: 单次否定 → 负证据保留 (不删, 防误伤)
    kb.feedback(TEST_USER, "不是这样的，我没学吉他", ["t2"])
    m2 = col.get(ids=["t2"])["metadatas"][0]
    assert m2["evidence"] < 0 and m2["status"] == mk.STATUS_PENDING, m2
    print("C 单次否定负证据保留  OK")
    # D: 两次否定 → 删除
    kb.feedback(TEST_USER, "都说了不是！", ["t2"])
    assert len(col.get(ids=["t2"])["ids"]) == 0, "两次否定应删除"
    print("D 两次否定删除  OK")


def test_ignore_and_render():
    m, kb, col = setup()
    # E: 忽略衰减
    kb.feedback(TEST_USER, "哈哈今天天气不错", ["t3"])
    m3 = col.get(ids=["t3"])["metadatas"][0]
    assert m3["evidence"] == -0.2, m3
    print("E 忽略衰减  OK")
    # F: 浮出标注: pending 渲染带 (还不太确定)
    r = kb.retrieve("累", n=1, user=TEST_USER)
    assert r and "(还不太确定)" in r[0]["text"], r
    print("F 浮出标注 pending  OK")


if __name__ == "__main__":
    test_confirm_promote()
    test_deny()
    test_ignore_and_render()
    print("\n知识页反馈闭环全部通过")
