# -*- coding: utf-8 -*-
"""ToolRouter — 工具选择与工具知识解耦 (2026-08-21 雾弥方案)
=====================================================================
模型不再需要认识 30 个工具名 — 只输出需求 intent, Router 负责匹配工具。
新工具注册进工具库即可, 模型免重训。

实现: bge-small-zh embedding 语义匹配 (本地, 免费, 快)
  intent → 向量 → 与每个工具描述向量余弦相似度 → top_k 命中

用法 (独立验证, 暂不接入生产):
  router = ToolRouter()
  router.resolve("查一下武汉明天的天气")
  → [('weather', 0.87), ('bili_search', 0.52), ...]
=====================================================================
"""
import os
import numpy as np

from memory import BgeEmbedding


class ToolRouter:
    # ★ 意图示例 (每个工具挂"用户会怎么说"的示例 — 比功能描述匹配准得多)
    #   工具向量 = 功能描述 + 多个用户示例; 匹配取每工具最大相似度
    _INTENT_EXAMPLES = {
        "look_around": ["看看D盘里有什么", "这个文件夹里是什么", "看一下项目目录", "C盘里有什么", "桌面有什么文件"],
        "find_file": ["帮我找某个文件", "找到那个配置文件", "电脑里有没有xxx", "这个文件在哪", "搜索一下文件"],
        "check_system": ["电脑卡不卡", "内存用了多少", "系统状态怎么样", "磁盘空间够吗", "现在几点了"],
        "goal_create": ["我想学会做饭，记下来", "立个目标", "帮我记住要做xxx", "决定学吉他"],
        "goal_list": ["我有什么目标", "进行中的目标有哪些", "我的目标进度"],
        "goal_done": ["目标完成了", "做完了，标记一下", "这个目标达成了"],
        "goal_drop": ["这个目标不做了", "放弃它"],
        "goal_update": ["更新目标进度", "这个目标进展到哪了"],
        "run_code": ["测试我写的工具", "跑一下代码", "验证能不能跑", "试试这个工具"],
        "create_tool": ["写个新工具", "创建工具", "写一段代码", "做个新功能"],
        "list_creations": ["我写过什么工具", "我的创作列表", "创作区有什么"],
        "memory_recall": ["记忆里有没有咖啡", "我记得什么", "查一下我的记忆", "我们上次聊到哪"],
        "memory_store": ["帮我记住这件事", "记一下xxx", "记住我的喜好"],
        "memory_status": ["记忆库有多少条", "我的记忆统计"],
        "emotion_state": ["我现在心情怎么样", "我的情绪状态"],
        "emotion_adjust": ["帮我调整情绪", "心情不好，调一下"],
        "weather": ["武汉明天天气", "今天下雨吗", "北京气温", "查天气"],
        "search": ["帮我搜一下脉冲神经网络", "查资料", "搜索一下xxx", "百度一下"],
        "bili_hot": ["B站今天有什么好玩的", "B站热门视频"],
        "bili_rank": ["B站知识区排行", "游戏区有什么好看的"],
        "bili_search": ["B站搜一下猫", "B站找xx视频"],
        "zhihu_daily": ["知乎今天有什么", "知乎日报"],
        "sspai_feed": ["少数派有什么文章", "科技效率文章"],
        "read_file": ["读一下这个文件", "打开配置文件看看", "读代码"],
        "grep_file": ["在代码里找timeout", "搜报错在哪一行"],
        "weather": ["武汉明天天气", "今天下雨吗", "北京气温"],
    }

    def __init__(self, tools=None):
        """tools: [{name, description, args_schema}] 工具清单 (缺省从 kiri_agent_tools 拉)"""
        self.emb = None          # 懒加载 (bge 模型 ~100MB, 首次用才加载)
        self.tool_descs = {}     # name -> 描述文本
        self.tool_vecs = {}      # name -> 向量列表 (描述 + 示例)
        if tools is None:
            self._load_default_tools()
        else:
            self._register(tools)

    def _load_default_tools(self):
        import kiri_agent_tools as kat
        tools, _, _ = kat.build_all_tools(None)
        self._register(tools)

    def _register(self, tools):
        for t in tools or []:
            name = t.get("name", "")
            desc = t.get("description", "")
            args = t.get("args_schema") or ""
            text = f"{name}: {desc}。参数: {args}" if args else f"{name}: {desc}"
            self.tool_descs[name] = text

    def _ensure_emb(self):
        if self.emb is None:
            self.emb = BgeEmbedding()
            # 注册时算好工具向量 (一次性, 缓存): 描述 + 意图示例 (每工具取最大相似度)
            for name, text in self.tool_descs.items():
                texts = [text] + self._INTENT_EXAMPLES.get(name, [])
                self.tool_vecs[name] = [np.array(v) for v in self.emb.embed_query(texts)]

    def resolve(self, intent, top_k=3, threshold=0.5):
        """intent → 最匹配的工具列表 [(name, score)]
        score < threshold 时返回空 (未命中 → 让模型重新描述或走 coding MCP)"""
        self._ensure_emb()
        q = np.array(self.emb.embed_query(str(intent)))
        scored = []
        for name, vecs in self.tool_vecs.items():
            # 每工具取最大相似度 (描述或任一示例)
            score = max(float(np.sum(q * v)) for v in vecs)
            if score >= threshold:
                scored.append((name, round(score, 3)))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def resolve_best(self, intent, threshold=0.5):
        """最匹配工具名 (未命中返回 None)"""
        r = self.resolve(intent, top_k=1, threshold=threshold)
        return r[0] if r else None

    # ---- LLM 路由 (2026-08-21 雾弥: 用 DS V4 Flash 做 router, 比 embedding 准) ----
    _ROUTER_SYS = """你是工具路由器。根据用户的需求描述, 从给定的工具候选中选择最合适的工具, 并提取调用参数。
规则:
- 只从候选列表里选, 不要发明工具
- ★参数名必须严格使用工具定义里的参数名 (如 find_file 用 keyword 不是 filename; 每个工具后面标注了参数名)
- 参数从需求里提取 (如城市/日期/关键词/路径), 没有就留空
- 如果没有合适的工具 (需求不是任何候选能处理的), 输出 {"tool": null, "reason": "简短原因"}
- 只输出JSON, 不要其他文字
格式: {"tool": "工具名", "args": {"参数名": "值"}, "reason": "为什么选它(≤15字)"}"""

    def resolve_llm(self, intent, candidates=None, use_engine=True):
        """LLM 路由: embedding 粗筛候选 (可选) → V4 Flash 精挑 + 提参
        返回: {"tool": str|None, "args": dict, "reason": str, "score": float}"""
        if candidates is None:
            # embedding 粗筛 top5 (免费, 缩小候选)
            cand = self.resolve(intent, top_k=5, threshold=0.45)
            candidates = [c[0] for c in cand]
        if not candidates:
            candidates = list(self.tool_descs.keys())  # 粗筛空 → 全清单
        # 候选描述 (精简: 只传候选的工具名+描述+参数名)
        lines = []
        for name in candidates:
            desc = self.tool_descs.get(name, name)
            lines.append(f"- {desc[:110]}")
        user = f"工具候选:\n" + "\n".join(lines) + f"\n\n用户需求: {intent}\n\n只输出JSON:"
        try:
            import engine
            import re
            import json as _json
            for _attempt in range(2):   # ★ 解析失败重试一次 (LLM偶发输出残缺JSON)
                raw = engine.generate_api(self._ROUTER_SYS, user, max_tokens=200, temperature=0.1)
                m = re.search(r"\{.*\}", raw, re.S)
                if not m:
                    continue
                try:
                    d = _json.loads(m.group(0))
                    break
                except Exception:
                    d = None
            if not d:
                return {"tool": None, "args": {}, "reason": "router解析失败", "score": 0.0}
            tool = d.get("tool")
            args = d.get("args") or {}
            return {"tool": tool if tool in self.tool_descs else None,
                    "args": args if isinstance(args, dict) else {},
                    "reason": str(d.get("reason", ""))[:30], "score": 1.0}
        except Exception as e:
            return {"tool": None, "args": {}, "reason": f"router错误:{e}", "score": 0.0}


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    router = ToolRouter()
    print(f"工具库: {len(router.tool_descs)} 个工具")
    tests = [
        "查一下武汉明天的天气",
        "帮我搜一下脉冲神经网络是什么",
        "B站今天有什么好玩的",
        "看看D盘里有什么",
        "读一下这个配置文件",
        "找找电脑上某个文件",
        "知乎今天有什么有意思的",
        "我现在心情怎么样",
        "帮我查查我的记忆里有没有咖啡",
        "把学会做饭记成目标",
        "测试一下我写的工具",
        "写一个新的工具",
        "今天适合穿什么衣服",
        "给我讲个笑话",
    ]
    for t in tests:
        r = router.resolve(t, top_k=3)
        best = r[0] if r else None
        print(f"  「{t}」 → {best}")
        if len(r) > 1:
            print(f"      (次选: {[f'{n}:{s}' for n, s in r[1:]]})")
