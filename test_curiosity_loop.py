# -*- coding: utf-8 -*-
"""test_curiosity_loop.py — agentic好奇循环逻辑测试 (mock LLM/MCP, 不花token)
场景: A满意即停 B换词继续 C工具失败放弃 D死循环防护(新词为空/重复)
"""
import sys
import os
import json
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reverie as reverie_mod

# ---- mock 记录器 ----
tool_calls = []     # (method, query)
encodes = []        # 入记忆的内容
events = []         # _log_event 调用
generate_seq = []   # engine.generate 返回序列


class MockKiri:
    class memory:
        @staticmethod
        def encode(s, e):
            encodes.append(s)
    class state:
        class emotion:
            state = {}
    def _log_event(self, kind, **kw):
        events.append((kind, kw))


def mock_generate(system, user, **kw):
    return generate_seq.pop(0)


def mock_call_tool(name, args):
    method = "ask_ai" if name == "ask_ai" else "search"
    tool_calls.append((method, args.get("query") or args.get("question") or ""))
    return tool_results.pop(0)


def setup(gen_seq, tool_seq):
    global generate_seq, tool_results, tool_calls, encodes, events
    generate_seq = list(gen_seq)
    tool_results = list(tool_seq)
    tool_calls = []
    encodes = []
    events = []
    reverie_mod.engine.generate = mock_generate
    # 函数内 import 的模块, 塞 sys.modules
    fake_mcp = types.ModuleType("mcp_client")
    fake_mcp.call_tool = mock_call_tool
    sys.modules["mcp_client"] = fake_mcp


def run(thoughts):
    k = MockKiri()
    rev = reverie_mod.ReverieEngine(k)
    rev._curiosity(thoughts)
    return rev


# ---- 场景A: 第一轮就满意 → 只调1次工具, 入记忆 ----
def test_satisfied():
    setup(
        [json.dumps({"question": "为什么人激动时沉默", "keywords": "人 情绪 沉默 心理学", "method": "search"}, ensure_ascii=False),
         json.dumps({"verdict": "satisfied", "note": "够了"}, ensure_ascii=False)],
        ["心理学研究表明情绪激动时人会暂时失去语言组织能力……（详细资料）"])
    run([{"text": "我好奇人激动的时候为什么反而说不出话"}])
    assert len(tool_calls) == 1, f"A: 工具调用应1次, 实际{len(tool_calls)}"
    assert len(encodes) == 1, f"A: 应入记忆1条, 实际{len(encodes)}"
    print("A 满意即停 ✓ (1轮, 入记忆)")


# ---- 场景B: 第一轮泛泛 → continue换词 → 第二轮满意 ----
def test_continue():
    setup(
        [json.dumps({"question": "为什么人激动时沉默", "keywords": "人 情绪 沉默", "method": "search"}, ensure_ascii=False),
         json.dumps({"verdict": "continue", "new_keywords": "情感压抑 心理防御机制", "new_method": "search"}, ensure_ascii=False),
         json.dumps({"verdict": "satisfied", "note": "这回清楚了"}, ensure_ascii=False)],
        ["泛泛的搜索结果: 情绪相关的通用介绍……",
         "情感压抑与心理防御机制的深度资料：人在强烈情绪下会启动防御……"])
    run([{"text": "我好奇人激动的时候为什么反而说不出话"}])
    assert len(tool_calls) == 2, f"B: 工具调用应2次, 实际{len(tool_calls)}"
    assert tool_calls[1][1] == "情感压抑 心理防御机制", f"B: 第二轮应换新词, 实际{tool_calls[1][1]}"
    assert len(encodes) == 1, f"B: 应入记忆1条(最后结果), 实际{len(encodes)}"
    print("B 换词继续 ✓ (2轮, 第二轮用新关键词, 入记忆)")


# ---- 场景C: 工具失败 → 诚实放弃, 不入记忆 ----
def test_abandon_fail():
    setup(
        [json.dumps({"question": "为什么人激动时沉默", "keywords": "人 情绪 沉默", "method": "search"}, ensure_ascii=False)],
        [None])   # 工具返回 None
    run([{"text": "我好奇"}])
    assert len(tool_calls) == 1
    assert len(encodes) == 0, f"C: 工具失败不应入记忆, 实际{len(encodes)}"
    print("C 工具失败放弃 ✓ (不入记忆)")


# ---- 场景D: continue 但新关键词为空/重复 → 防死循环, 停止 ----
def test_loop_guard():
    setup(
        [json.dumps({"question": "为什么人激动时沉默", "keywords": "人 情绪 沉默", "method": "search"}, ensure_ascii=False),
         json.dumps({"verdict": "continue", "new_keywords": "", "new_method": "search"}, ensure_ascii=False)],
        ["搜索结果一……"])
    run([{"text": "我好奇"}])
    assert len(tool_calls) == 1, f"D: 新词为空应停在第1轮, 实际{len(tool_calls)}轮"
    assert len(encodes) == 0, f"D: 未查到不应入记忆"
    print("D 死循环防护 ✓ (新词为空 → 停, 不入记忆)")


if __name__ == "__main__":
    test_satisfied()
    test_continue()
    test_abandon_fail()
    test_loop_guard()
    print("\n全部通过 ✓")
