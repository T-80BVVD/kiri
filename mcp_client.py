# -*- coding: utf-8 -*-
"""Kiri MCP 客户端 (方案A: Kiri作为MCP client)
=====================================================================
轻量实现: 每次调用临时起连接 (stdio), 调用完即关。
代价: 每次调工具多~0.3s连接开销 — 联想引擎3分钟一次, 可接受。
优点: 无后台线程/事件循环管理, 简单可靠, 不跟Kiri的daemon抢loop。
工具调用记录: 每次调用写入 _CALLS (供监控面板显示)
=====================================================================
"""
import os
import sys
import time
import json
import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiri_mcp_server.py")
EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.jsonl")

# ★ 工具调用记录 (监控面板用, 最多留100条)
_CALLS = []


def _append_event(kind, **data):
    """工具调用落盘到 events.jsonl (与 Kiri 的事件日志同源, 永久记录)"""
    try:
        ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **data}
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_tool_calls(n=30):
    """最近 n 条工具调用记录 [{ts, tool, args, result}]"""
    return list(_CALLS[-n:])


async def _call(name, args):
    params = StdioServerParameters(command=sys.executable, args=[MCP_SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool(name, args or {})
            if r and r.content:
                parts = [c.text for c in r.content if hasattr(c, "text")]
                return "\n".join(parts) if parts else None
    return None


def call_tool(name, args=None):
    """同步调用 MCP 工具; 失败返回 None (不阻塞Kiri主流程异常)
    每次调用: 内存记录(监控面板) + events.jsonl落盘(永久日志)"""
    args = args or {}
    t0 = time.time()
    ok = False
    result = None
    try:
        result = asyncio.run(_call(name, args))
        ok = result is not None
    except Exception:
        result = None
    finally:
        lat = time.time() - t0
        rec = {
            "ts": time.strftime("%H:%M:%S"),
            "tool": name,
            "args": str(args)[:60],
            "result": (result or "")[:100],
            "ok": ok,
            "latency": round(lat, 2),
        }
        _CALLS.append(rec)
        if len(_CALLS) > 100:
            _CALLS.pop(0)
        # ★ 落盘: 工具调用进 events.jsonl (kind=tool_call, 永久记录)
        _append_event("tool_call", tool=name, args=str(args)[:60],
                      result=(result or "")[:200], ok=ok, latency=round(lat, 2))
        # ★ 心路日志: 工具调用
        try:
            import kiri_mind
            kiri_mind.tool_call(name, args, result or "", ok, lat)
        except Exception:
            pass
    return result


def selftest():
    print("=== MCP 客户端自测 (每调用独立连接) ===")
    w = call_tool("weather")
    print(f"天气: {w}")
    s = call_tool("search", {"query": "脉冲神经网络", "n": 2})
    print(f"搜索:\n{s}")
    print(f"调用记录: {len(get_tool_calls())} 条")
    print("✓ 自测完成" if w else "✗ 失败")


if __name__ == "__main__":
    selftest()
