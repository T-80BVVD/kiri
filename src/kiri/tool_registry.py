# -*- coding: utf-8 -*-
"""Kiri 工具注册表 (tool_registry.py) — 吸收 N.E.K.O ToolRegistry / @llm_tool 思想
=====================================================================
解决的问题:
- 旧实现: kiri.py 里 if/elif 手写 weather/search/ask_ai, 新增工具要改主流程
- NEKO 做法: 工具注册表 + 统一调用 + 结果回投, 插件/新工具只注册不侵入主循环

本模块:
- 保留 MCP 作为底层执行通道 (kiri_mcp_server.py)
- 新增工具只需要 register() 一个名字 + 参数映射, 不需要改 kiri.py
- invoke() 解析 LLM 的 [TOOL:name|param] 标记, 执行并记录
=====================================================================
"""
import time
import json
import os
import re

# 注册表: name -> {"handler": callable, "aliases": set, "description": str}
_TOOLS = {}


def register(name, handler, aliases=None, description=""):
    """注册一个工具。
    handler: 接收 (param: str) -> str, 或接收 (args: dict) -> str 的 callable。
    aliases: 可选别名列表, 让 LLM 写错时也能容错。
    """
    name = (name or "").strip().lower()
    if not name:
        raise ValueError("tool name is required")
    _TOOLS[name] = {
        "handler": handler,
        "aliases": {a.strip().lower() for a in (aliases or []) if a and a.strip()},
        "description": description,
    }
    return name


def unregister(name):
    name = (name or "").strip().lower()
    return _TOOLS.pop(name, None) is not None


def list_tools():
    """返回可给 LLM prompt 使用的工具清单。"""
    out = []
    for name, meta in sorted(_TOOLS.items()):
        aliases = ",".join(sorted(meta["aliases"])) if meta["aliases"] else ""
        desc = meta["description"] or name
        if aliases:
            out.append(f"- [TOOL:{name}|参数]  {desc} (别名: {aliases})")
        else:
            out.append(f"- [TOOL:{name}|参数]  {desc}")
    return "\n".join(out)


def resolve(name):
    """名称/别名 → 注册名, 找不到返回 None。"""
    name = (name or "").strip().lower()
    if name in _TOOLS:
        return name
    for reg_name, meta in _TOOLS.items():
        if name in meta["aliases"]:
            return reg_name
    return None


def invoke(spec):
    """执行 [TOOL:name|param] 标记。
    spec: 'name|param' 或 'name'。
    返回 (tool_name, result_str, ok_bool)。失败不抛异常。
    """
    spec = (spec or "").strip()
    if not spec:
        return None, "", False
    name, _, param = spec.partition("|")
    reg_name = resolve(name)
    if not reg_name:
        return name.strip().lower(), "", False
    handler = _TOOLS[reg_name]["handler"]
    param = param.strip()
    try:
        result = handler(param)
        result_str = str(result or "")[:2000]
        return reg_name, result_str, bool(result_str)
    except Exception as exc:
        return reg_name, f"工具执行失败: {exc}", False


# ---------------- MCP 桥接 ----------------

def _mcp_tool(tool_name, param_key, default=""):
    """生成一个把文本参数转成 dict 并调 MCP 的 handler。"""
    def handler(param):
        import mcp_client
        arg = param or default
        return mcp_client.call_tool(tool_name, {param_key: arg})
    return handler


def _weather_handler(param):
    """天气工具: 参数支持 '城市' 或 '城市 明天/后天' 或 '明天' (城市留空=自动定位)
    例: '' → 当前; '北京' → 北京当前; '明天' → 当前地点明天; '北京 后天' → 北京后天"""
    import mcp_client
    arg = (param or "").strip()
    day = "今天"
    city = ""
    # 提取日期词 (明天/明/后天/后/大后天)
    for d, key in (("大后天", "大后天"), ("后天", "后天"), ("明天", "明天"),
                   ("明", "明天"), ("后", "后天"), ("tomorrow", "明天")):
        if d in arg:
            day = key
            arg = arg.replace(d, " ").strip()
            break
    # 剩余为城市 (含空格/逗号分隔的复合城市名保留原样; 空=auto)
    city = arg or "auto"
    return mcp_client.call_tool("weather", {"city": city, "day": day})


def _mcp_noarg(tool_name, param_key=None):
    def handler(param):
        import mcp_client
        args = {param_key: param} if param_key and param else {}
        return mcp_client.call_tool(tool_name, args)
    return handler


# 内置注册 (与 kiri_mcp_server.py 对齐; 新增工具直接加这里)
register("weather", _weather_handler,
         aliases=["天气", "tianqi"],
         description="实时天气或预报。参数=城市名(可加'明天/后天', 如'北京 明天'; 留空=当前地点今天)")
register("search", _mcp_tool("search", "query"),
         aliases=["搜索", "搜", "查资料", "bing"],
         description="Bing 搜索网页资料。参数=搜索关键词")
register("ask_ai", _mcp_tool("ask_ai", "question"),
         aliases=["问AI", "ai", "导师"],
         description="问一个博学多识的AI导师。参数=问题")
register("bili_hot", _mcp_noarg("bili_hot"),
         aliases=["b站热门", "bilihot"],
         description="看B站热门视频。参数可留空")
register("bili_rank", _mcp_tool("bili_rank", "zone", "知识"),
         aliases=["b站分区", "bilibili分区"],
         description="看B站某分区排行榜。参数=分区名(知识/游戏/鬼畜/数码/生活等)")
register("bili_search", _mcp_tool("bili_search", "keyword"),
         aliases=["b站搜索", "搜b站"],
         description="在B站搜索视频。参数=搜索词")
register("zhihu_daily", _mcp_noarg("zhihu_daily"),
         aliases=["知乎日报", "zhihu"],
         description="看知乎日报(每日精选)。参数可留空")
register("sspai_feed", _mcp_noarg("sspai_feed"),
         aliases=["少数派", "sspai"],
         description="看少数派最新文章。参数可留空")
register("snapshot", _mcp_noarg("snapshot"),
         aliases=["世界快照", "环境"],
         description="外部世界快照(当前天气)。参数可留空")
# ★ 探索自由 (2026-08-20 雾弥授权): 电脑是她的家, 她可以自由走动看东西 (全部只读)
register("look_around", _mcp_tool("look_around", "path", "D:\\"),
         aliases=["看看", "看看文件夹", "家里有什么", "看看房间"],
         description="看看某个文件夹里有什么。参数=文件夹路径(默认D盘)。像在家走动看房间")
register("read_file", _mcp_tool("read_file", "path"),
         aliases=["读文件", "看看文件", "翻笔记"],
         description="读一个文本文件的内容。参数=文件完整路径。只读文本, 返回前3000字")
register("find_file", _mcp_tool("find_file", "keyword"),
         aliases=["找文件", "找找", "找东西", "搜索文件"],
         description="按名字关键词找文件。参数=关键词。从D盘开始找")
register("check_system", _mcp_noarg("check_system"),
         aliases=["看系统", "电脑状态", "看看电脑"],
         description="看看电脑现在的状态(时间/内存/磁盘/进程)。参数可留空")
register("read_self", _mcp_noarg("read_self"),
         aliases=["看自己", "我是谁", "我的档案", "读自己的档案"],
         description="看看自己是什么(读架构文档)。参数可留空")


def selftest():
    """本地自测: 不真正调网络, 只验证解析/注册/未知工具。"""
    assert resolve("天气") == "weather"
    assert resolve("bilihot") == "bili_hot"
    assert resolve("nope") is None
    name, result, ok = invoke("weather|北京")
    assert name == "weather"
    name2, result2, ok2 = invoke("does_not_exist|foo")
    assert not ok2
    print("tool_registry selftest PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
