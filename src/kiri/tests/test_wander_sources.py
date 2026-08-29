# -*- coding: utf-8 -*-
"""test_wander_sources.py — 漫游多源化逻辑测试 (mock mcp_client, 不联网不花token)
场景: A 多源收集失败容忍 B 防重复硬窗口 C 公平轮换不霸榜 D 概率恢复
"""
import sys
import os
import time
import types

# Windows 控制台默认GBK, print含会崩 → 强制UTF-8 (与 qq_bridge 同法)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wander_sources as ws

# ---- mock: 内容源返回 (按工具名) ----
_tool_results = {}


def mock_call_tool(name, args):
    return _tool_results.get(name)


def setup():
    fake_mcp = types.ModuleType("mcp_client")
    fake_mcp.call_tool = mock_call_tool
    sys.modules["mcp_client"] = fake_mcp
    # 独立 dedup 存储 (不污染真库)
    _test_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_dedup.json")
    d = ws.DedupStore(_test_path)
    d._data.clear()
    d.save()
    return d


# ---- 场景A: 单源失败不阻塞整体 ----
def test_source_failure_tolerant():
    global _tool_results
    _tool_results = {"bili_rank": "- [知识] 量子计算入门 by UP主 播放1000",
                     "zhihu_daily": "- 为什么人激动时会沉默"}
    # bili_hot 失败 (不在mock里→None)
    results = ws.collect(topic="量子", zone="知识", max_sources=3)
    ok = [r for r in results if r]
    assert len(ok) >= 2, f"A: 应有≥2个成功源, 实际{len(ok)}"
    print(f"A 失败容忍  (成功{len(ok)}/{len(results)})")


# ---- 场景B: 5h 硬窗口防重复 ----
def test_hard_window():
    dedup = setup()
    e = {"title": "量子计算入门", "url": "https://www.bilibili.com/video/BV1"}
    assert not dedup.should_skip(e), "新条目不应跳过"
    dedup.mark(e)
    assert dedup.should_skip(e), "5h内必跳"
    # 模拟 24h 后: 半衰期36h → p_recover≈0.63
    dedup._data[dedup._key(e)]["last_seen"] = time.time() - 24 * 3600
    # 大概率仍跳 (随机, 不硬断言; 只验证概率恢复逻辑不崩)
    dedup.should_skip(e)
    print("B 硬窗口 ")


# ---- 场景C: 公平轮换 ----
def test_round_robin():
    dedup = setup()
    srcs = [
        {"source": "s1", "label": "L1", "text": "- a1\n- a2\n- a3"},
        {"source": "s2", "label": "L2", "text": "- b1\n- b2\n- b3"},
    ]
    cands = ws.pick_candidates(srcs, dedup, budget=6)
    s1 = [c for c in cands if c["source"] == "s1"]
    s2 = [c for c in cands if c["source"] == "s2"]
    assert len(s1) == 3 and len(s2) == 3, f"C: 应均分(3/3), 实际{len(s1)}/{len(s2)}"
    print("C 公平轮换  (3/3)")


# ---- 场景D: 被选候选的 URL 提取 ----
def test_link_extract():
    assert ws._extract_links("bili_hot", "视频A (https://www.bilibili.com/video/BV1xx)") == \
        [{"url": "https://www.bilibili.com/video/BV1xx", "source": "bili_hot"}]
    items = ws._split_items({"text": "- 视频A (https://a.com)\n- 视频B"})
    assert items[0]["url"] == "https://a.com"
    assert items[1]["url"] is None
    print("D 链接提取 ")


if __name__ == "__main__":
    setup()
    test_source_failure_tolerant()
    test_hard_window()
    test_round_robin()
    test_link_extract()
    # 清理测试文件
    try:
        os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_dedup.json"))
    except Exception:
        pass
    print("\n全部通过 ")
