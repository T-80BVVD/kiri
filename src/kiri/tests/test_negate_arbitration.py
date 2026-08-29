# -*- coding: utf-8 -*-
"""test_negate_arbitration.py — 记忆纠错 LLM 仲裁测试 (NEKO corrections吸收)
场景: A 仲裁只标被否定的候选 (不误伤无关记忆)
      B 降级: LLM失败 → 旧行为全部标记 (不阻塞)
      C 仲裁返回格式错误 → 降级
mock engine.generate, 不花token; 独立测试集合, 测完删除
"""
import sys
import os
import time
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory as memory_mod

TEST_USER = "_p7btest"
_gen_seq = []
_gen_fail = False


def fake_generate(system, user, **kw):
    if _gen_fail:
        raise RuntimeError("api down")
    return _gen_seq.pop(0)


def setup():
    global _gen_seq, _gen_fail
    _gen_seq = []
    _gen_fail = False
    fake = types.ModuleType("engine")
    fake.generate = fake_generate
    sys.modules["engine"] = fake
    m = memory_mod.Memory()
    col = m.get_user_collection(TEST_USER)
    try:
        data = col.get(limit=col.count() or 1)
        if data and data.get("ids"):
            col.delete(ids=data["ids"])
    except Exception:
        pass
    for tid, txt in [("a1", "雾弥说: 我喜欢黑色"),
                     ("a2", "雾弥说: 我在学吉他"),
                     ("a3", "雾弥说: 今天加班很累")]:
        col.add(ids=[tid], documents=[txt],
                metadatas=[{"timestamp": time.time(), "salience": 0.5,
                            "source": "user_observation", "session": "global",
                            "speaker": "user", "disp": 0.0}])
    return m, col


def test_arbitrate_precise():
    m, col = setup()
    _gen_seq.append('{"corrected": [true, false, false]}')
    n = m.negate("雾弥说: 我不喜欢黑色", TEST_USER)
    assert n == 1, f"A: 应只标1条, 实际{n}"
    assert col.get(ids=["a1"])["metadatas"][0].get("corrected")
    assert col.get(ids=["a1"])["metadatas"][0].get("disp") == 1.0
    assert not col.get(ids=["a2"])["metadatas"][0].get("corrected"), "吉他不应误伤"
    print("A 仲裁精确标记 OK (黑色标错, 吉他未误伤)")


def test_fallback_all():
    m, col = setup()
    _gen_fail = True
    n = m.negate("雾弥说: 我不喜欢加班", TEST_USER)
    assert n >= 1, f"B: 降级应标记, 实际{n}"
    assert col.get(ids=["a3"])["metadatas"][0].get("corrected")
    print("B LLM失败降级全部标记 OK")


def test_fallback_bad_format():
    m, col = setup()
    _gen_seq.append("不是JSON的回复")
    n = m.negate("雾弥说: 我不喜欢加班", TEST_USER)
    assert n >= 1, f"C: 格式错误应降级, 实际{n}"
    print("C 仲裁格式错误降级 OK")


if __name__ == "__main__":
    test_arbitrate_precise()
    test_fallback_all()
    test_fallback_bad_format()
    m, col = setup()
    m.client.delete_collection(col.name)
    print("\n纠错仲裁全部通过")
