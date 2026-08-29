# -*- coding: utf-8 -*-
"""test_agent_loop.py — agent 循环引擎测试 (mock 决策, 含流式)
验证: 流式决策解析 / 工具执行回填 / 多轮循环 / 最终回复 / 兜底"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent
import engine as engine_mod

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


def mock_engine(scripted, repeat=False):
    """同时 mock generate(非流式) 和 generate_stream(流式)"""
    idx = {"i": 0}

    def _next():
        if repeat:
            return scripted[idx["i"] % len(scripted)]
        return scripted[idx["i"]] if idx["i"] < len(scripted) else "(……"

    def gen(sys_p, user_p, **kw):
        r = _next()
        idx["i"] += 1
        return r

    def gen_stream(sys_p, user_p, **kw):
        r = _next()
        idx["i"] += 1
        for ch in r:
            yield ch

    engine_mod.generate = gen
    engine_mod.generate_stream = gen_stream


def test_decision_call_then_reply():
    """场景1: 先调工具(拿结果) → 再回复 (流式决策 + 长文路径)"""
    calls = []
    mock_engine([
        '{"action": "call", "tool": "emotion_state", "args": {}}',
        '{"action": "reply"}',   # 决策不带text → 走长文独立生成
        '（查了下心情）现在挺平静的。',  # 长文生成的返回
    ])

    def exec_fn(tool, args):
        calls.append(tool)
        return "心情 0.1 | 愉悦 0.2 | 无聊 0.3"

    loop = agent.AgentLoop("你是Kiri测试", [{"name": "emotion_state", "description": "查情绪"}], exec_fn)
    out = loop.run("你现在心情怎么样", max_rounds=4)
    check("先调了情绪工具", calls == ["emotion_state"], str(calls))
    check("再给出回复", "平静" in out, out)


def test_tool_fail_then_reply():
    """场景2: 工具失败 → LLM 换策略直接回复"""
    mock_engine([
        '{"action": "call", "tool": "weather", "args": {"city": "auto"}}',
        '{"action": "reply"}',
        '天气工具好像坏了，我查不到。',
    ])
    loop = agent.AgentLoop("你是Kiri", [{"name": "weather", "description": "天气"}],
                           lambda t, a: "天气获取失败: timeout")
    out = loop.run("今天天气")
    check("失败后如实说", "坏" in out or "查不到" in out, out)


def test_parse_bad_json():
    """场景3: LLM 返回非JSON → 原文当回复兜底"""
    mock_engine(["（尾巴晃了晃）不想用工具，直接说。"], repeat=True)
    loop = agent.AgentLoop("你是Kiri", [], lambda t, a: "")
    out = loop.run("hi")
    check("非JSON原文兜底", "不想用工具" in out, out)


def test_max_rounds():
    """场景4: 一直调工具 → 轮数上限兜底"""
    mock_engine(['{"action": "call", "tool": "loop", "args": {}}'] * 10)
    loop = agent.AgentLoop("你是Kiri", [{"name": "loop", "description": "x"}],
                           lambda t, a: "又一轮")
    out = loop.run("go", max_rounds=3)
    check("轮数上限兜底", "想了太久" in out, out)


def test_stream_decision_truncated():
    """场景5: 流式被截断 (JSON不完整) → 重试非流式拿到决策"""
    # 流式拿到不完整JSON (模拟截断), 非流式重试拿到完整
    mock_engine(['{"action": "call", "tool": "read_file", "args": {"path": "x"}',
                 '{"action": "reply"}',
                 '读完了'])
    loop = agent.AgentLoop("你是Kiri", [{"name": "read_file", "description": "读"}],
                           lambda t, a: "内容: x")
    out = loop.run("读文件", max_rounds=4)
    check("截断后重试成功", "读完了" in out, out)


test_decision_call_then_reply()
test_tool_fail_then_reply()
test_parse_bad_json()
test_max_rounds()
test_stream_decision_truncated()
print(f"\n通过 {len(PASS)}/{len(PASS)+len(FAIL)}")
sys.exit(0 if not FAIL else 1)
