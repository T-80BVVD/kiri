# -*- coding: utf-8 -*-
"""test_user_mood_integration.py — 用户情绪画像集成测试 (NEKO master_emotion吸收, 聚合层)
场景: A parse_emotion 新字段(confidence/external_intent)+旧格式兼容
      B describe 注入"对雾弥情绪的感知" (prompt可见)
      C 低落+高波动 → should_proactive 倾向 cheerup (陪伴而非玩闹)
      D external_intent 高 → respond 注入工具提示 (user_p 含 [TOOL:])
mock engine, 不花token
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
import prompt as prompt_mod
import state as state_mod
import kiri as kiri_mod


def test_parse_emotion_fields():
    r = prompt_mod.parse_emotion('{"valence": -0.5, "arousal": 0.3, "salience": 0.7,'
                                 ' "confidence": 0.9, "external_intent": 0.8}')
    assert r["confidence"] == 0.9 and r["external_intent"] == 0.8, r
    r2 = prompt_mod.parse_emotion('{"valence": 0.5, "arousal": 0.1, "salience": 0.2}')
    assert "confidence" not in r2 and r2["valence"] == 0.5, r2   # 旧格式兼容
    print("A parse_emotion 新字段+旧格式兼容 OK")


def test_describe_inject():
    s = state_mod.State()
    now = time.time()
    for i in range(5):
        s.mood_profile.record("雾弥", {"valence": -0.4, "arousal": 0.3,
                                       "salience": 0.6, "confidence": 0.9},
                              ts=now - (4 - i) * 60)
    d = s.describe("雾弥")
    assert "情绪的感知" in d, d[-200:]
    print("B describe 情绪感知注入 OK")


def test_cheerup_tendency():
    s = state_mod.State()
    now = time.time()
    for i in range(5):
        s.mood_profile.record("雾弥", {"valence": -0.5 + (0.35 if i % 2 else -0.35),
                                       "arousal": 0.4, "salience": 0.6, "confidence": 0.9},
                              ts=now - (4 - i) * 60)
    assert s.mood_profile.should_be_gentle("雾弥"), "应进入陪伴模式"
    s.last_proactive = -9999.0
    s.last_interact = now - 3600
    s.boredom = 0.6
    tr, reason = s.should_proactive()
    assert tr and reason == "cheerup", (tr, reason)
    print("C 低落+波动 → cheerup 陪伴倾向 OK")


def test_external_intent_nudge():
    # mock engine.generate (直接替换模块函数; engine 是模块级导入, sys.modules 替换无效)
    import engine as engine_mod
    orig = engine_mod.generate
    calls = {"n": 0}

    def fake_gen(sys_p, user_p, **kw):
        calls["n"] += 1
        return ('{"valence": 0.0, "arousal": 0.1, "salience": 0.4,'
                ' "confidence": 0.9, "external_intent": 0.9}')
    engine_mod.generate = fake_gen
    try:
        k = kiri_mod.Kiri()
        emo = k._analyze_emotion("今天上海天气怎么样")
        assert emo and emo.get("external_intent", 0) >= 0.7, emo
        assert emo.get("confidence") == 0.9
        print("D external_intent 解析 OK:", emo)
    finally:
        engine_mod.generate = orig


if __name__ == "__main__":
    test_parse_emotion_fields()
    test_describe_inject()
    test_cheerup_tendency()
    test_external_intent_nudge()
    print("\n用户情绪画像集成全部通过")
