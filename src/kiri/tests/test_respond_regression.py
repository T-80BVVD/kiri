# -*- coding: utf-8 -*-
"""test_respond_regression.py — respond() 完整性回归测试
背景: 2026-08-19 6ebc047 把 _tool_result_invalid 静态方法插进了 respond() 方法体中间,
导致 respond() 在工具块后提前结束 — 防空兜底/防重复/心路日志/history/记忆编码/
respond事件日志/data_log 全部变成死代码; 工具重生成空时返回空串 (用户看到"无输出")。
验证:
  1. 工具调用成功 → 重生成有效 → 返回基于工具结果的回复 + 事件齐全
  2. 工具成功 + 重生成空 + 原文无正文 → 防空兜底 → 优雅降级 (绝不返回空/None/(……)
  3. 普通回复 (无工具) → 尾部逻辑完整 (respond事件 + data_log)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kiri as kiri_mod
import data_log as data_log_mod
import tool_registry

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


def make_kiri(generate_fn):
    """构造 Kiri, 替换 engine.generate 与 _log_event/data_log 为记录器
    ★ 同时 mock tool_registry.invoke: 避免测试触发真实网络工具调用
    ★ 防污染: _append_history_file / kiri_mind / memory.encode 一律落记录器, 不写生产文件"""
    k = kiri_mod.Kiri()
    kiri_mod.engine.generate = generate_fn  # mock 主引擎
    tool_registry.invoke = lambda spec: ("weather", "Yenchuang,China: 晴天25度", True)
    events = []
    k._log_event = lambda kind, **kw: events.append((kind, kw))  # 记录不落盘
    data_log_mod.respond = lambda **kw: events.append(("data_log", kw))
    k._record_output = lambda *a, **kwa: None
    k._append_history_file = lambda *a, **kwa: None          # 不写 history.jsonl
    k.memory.encode = lambda *a, **kwa: None                 # 不写 chromadb
    try:
        import kiri_mind
        kiri_mind.chat_user = lambda *a, **kwa: None
        kiri_mind.chat_kiri = lambda *a, **kwa: None
    except Exception:
        pass
    return k, events


def make_gen(primary, tool_regen=None, fallback="(……"):
    """按调用特征路由的 engine.generate (不依赖调用顺序):
    temperature=0.1 → 情绪分析; max_tokens无+temp0.7 → 主生成; 含[工具结果 → 重生成;
    防空(300,0.8) → fallback; 防重复/防话题(300,0.9) → 兜底文本"""
    def gen(sys_p, user_p, **kw):
        if kw.get("temperature") == 0.1:
            return "（平静）"                      # _analyze_emotion
        if kw.get("max_tokens") == 300 and kw.get("temperature") == 0.9:
            return "（换个说法）还是聊点别的吧。"     # 防重复/防话题复读
        if "工具结果" in user_p:
            return tool_regen if tool_regen is not None else "(……"   # 工具重生成
        if kw.get("temperature") == 0.8 and kw.get("max_tokens") == 300:
            return fallback                        # 防空兜底重试
        return primary                             # 主生成
    return gen


def main():
    print("=== 场景1: 工具调用成功 + 重生成有效 → 基于工具结果回复 ===")
    k, events = make_kiri(make_gen(
        primary="明天天气怎么样？[TOOL:weather|]",
        tool_regen="（翻了下手机）明天多云，25度左右，记得带伞。"))
    reply = k.respond("明天天气怎么样", "雾弥", scene="private")
    check("返回非空", bool(reply and reply.strip()), repr(reply))
    check("用了工具重生成结果", "带伞" in reply, repr(reply))
    kinds = [e[0] for e in events]
    check("工具调用事件已记录", any(e[0] == "tool_call" and e[1].get("ok") for e in events),
          str(kinds))
    check("respond事件已记录", "respond" in kinds, str(kinds))
    check("data_log已记录", "data_log" in kinds, str(kinds))

    print("=== 场景2: 工具成功 + 重生成空 + 原文无正文 → 防空兜底优雅降级 ===")
    k2, events2 = make_kiri(make_gen(
        primary="[TOOL:time|]",            # 只有工具调用, 剥标记后为空
        tool_regen="(……",
        fallback="(……"))
    reply2 = k2.respond("现在几点了", "雾弥", scene="private")
    check("降级非空", bool(reply2 and reply2.strip()), repr(reply2))
    check("降级不以(……开头", not reply2.startswith("(……"), repr(reply2))
    check("降级是优雅话术", "卡了一下" in reply2, repr(reply2))
    kinds2 = [e[0] for e in events2]
    check("降级也记录respond事件", "respond" in kinds2, str(kinds2))
    check("降级也记录data_log", "data_log" in kinds2, str(kinds2))

    print("=== 场景3: 普通回复 (无工具) → 尾部逻辑完整 ===")
    k3, events3 = make_kiri(make_gen(primary="（歪头）想吃火锅吗？"))
    reply3 = k3.respond("晚上吃啥", "雾弥", scene="private")
    check("普通回复非空", bool(reply3 and reply3.strip()), repr(reply3))
    check("普通回复只取第一句", "\n" not in reply3, repr(reply3))
    kinds3 = [e[0] for e in events3]
    check("普通回复也记录respond事件", "respond" in kinds3, str(kinds3))
    check("普通回复也记录data_log", "data_log" in kinds3, str(kinds3))

    print(f"\n通过 {len(PASS)}/{len(PASS)+len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
