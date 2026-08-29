# -*- coding: utf-8 -*-
"""test_trust_recall.py — 跨用户记忆信任加权测试 (NEKO trust_store吸收)
场景: 两个其他用户都有相似记忆, 查询方问"谁说过X":
  A 高信任用户(1.0)的记忆在跨用户召回中胜过低信任用户(0.3)
  B 不传 trust_weights 时两者权重相同 (缺省0.5, 兼容旧行为)
用独立测试用户 (不碰雾弥真实库), 测完删除
"""
import sys
import os
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory as memory_mod

QUERIER = "_trust_querier"
HIGH = "_trust_high"      # 信任 1.0
LOW = "_trust_low"        # 信任 0.3
SAME_TEXT = "说: 我喜欢黑色"


def setup():
    m = memory_mod.Memory()
    cols = {}
    for u in (QUERIER, HIGH, LOW):
        col = m.get_user_collection(u)
        try:
            data = col.get(limit=col.count() or 1)
            if data and data.get("ids"):
                col.delete(ids=data["ids"])
        except Exception:
            pass
        cols[u] = col
    # 查询方: 无关记忆 (逼出跨用户保底)
    cols[QUERIER].add(ids=["q1"], documents=["说: 今天天气不错"],
                      metadatas=[{"timestamp": time.time(), "salience": 0.5,
                                  "session": "global", "speaker": "user",
                                  "source": "user_observation"}])
    # 两个其他用户: 几乎相同的记忆
    for u, tid in ((HIGH, "h1"), (LOW, "l1")):
        cols[u].add(ids=[tid], documents=[f"{u}{SAME_TEXT}"],
                    metadatas=[{"timestamp": time.time(), "salience": 0.5,
                                "session": "global", "speaker": "user",
                                "source": "user_observation"}])
    return m


def _cross_user_texts(results):
    return [x["text"] for x in results if x.get("cross_user")]


def test_trust_weighting():
    m = setup()
    # A: 高信任记忆应胜出
    res = m.retrieve("有人说过喜欢黑色吗", current_mood=0.0, n=4,
                     user=QUERIER, trust_weights={HIGH: 1.0, LOW: 0.3})
    cross = _cross_user_texts(res)
    assert cross, f"A: 应有跨用户记忆, 实际{res}"
    top = cross[0]
    assert HIGH in top, f"A: 高信任记忆应排前, 实际[{top[:30]}]"
    print(f"A 高信任胜出 OK (跨用户{len(cross)}条, 第一条来自{HIGH})")
    # B: 不传 trust_weights → 缺省相同权重, 排序看谁的相关性/插入序
    res2 = m.retrieve("有人说过喜欢黑色吗", current_mood=0.0, n=4, user=QUERIER)
    cross2 = _cross_user_texts(res2)
    assert cross2, "B: 应有跨用户记忆"
    print("B 缺省权重兼容 OK (旧行为不破坏)")
    m.client.delete_collection(m.get_user_collection(QUERIER).name)
    m.client.delete_collection(m.get_user_collection(HIGH).name)
    m.client.delete_collection(m.get_user_collection(LOW).name)
    m._collections.clear()
    m._thought_cols.clear()


if __name__ == "__main__":
    test_trust_weighting()
    print("\ntrust 信任加权全部通过")
