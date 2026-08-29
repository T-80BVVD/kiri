# -*- coding: utf-8 -*-
"""Kiri 外部感知工具集 (tools) — 让她接触真实世界, 给联想引擎提供外部刺激
=====================================================================
当前工具 (全部只读, 安全):
  - search(): Bing 搜索, 查资料 → 结果入记忆
  - weather(): wttr.in 实时天气 → 她感知到"外面在下雨/晴天/很冷"
  - (未来扩展: 读文件/执行命令等, 需用户明确授权)
网络现状: DuckDuckGo被墙; Bing/Bilibili/wttr.in 可用
=====================================================================
"""
import re
import html
import urllib.request
import urllib.parse
import socket
import json

socket.setdefaulttimeout(10)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------- 搜索 (Bing) ----------------
def search(query, n=3):
    """Bing 搜索 → 提取标题+摘要列表; 失败返回空列表"""
    try:
        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
        page = _get(url)
    except Exception:
        return []
    # Bing 结果: <li class="b_algo"><h2><a>标题</a></h2><p>摘要</p>
    items = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', page, re.S):
        block = m.group(0)
        t = re.search(r'<h2[^>]*><a[^>]*>(.*?)</a>', block, re.S)
        p = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        title = html.unescape(re.sub(r'<[^>]+>', '', t.group(1))).strip() if t else ""
        desc = html.unescape(re.sub(r'<[^>]+>', '', p.group(1))).strip() if p else ""
        if title:
            items.append({"title": title[:80], "desc": desc[:200]})
        if len(items) >= n:
            break
    return items


# ---------------- 天气 (wttr.in) ----------------
def weather(city="auto"):
    """实时天气 JSON → 摘要; 失败返回 None
    wttr.in 自动按IP定位(city=auto)或指定城市"""
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
        data = json.loads(_get(url))
        cur = data["current_condition"][0]
        temp = cur["temp_C"]
        desc = cur["lang_zh"][0]["value"] if "lang_zh" in cur and cur["lang_zh"] else cur["weatherDesc"][0]["value"]
        hum = cur["humidity"]
        feels = cur["FeelsLikeC"]
        return {"temp_c": temp, "feels_c": feels, "humidity": hum, "desc": desc}
    except Exception:
        return None


# ---------------- 汇总格式化 (给联想引擎用) ----------------
def world_snapshot():
    """外部世界快照: 天气 + 简单文本; 失败部分跳过"""
    parts = []
    w = weather()
    if w:
        parts.append(f"外面{['未知','晴','多云','阴','小雨','大雨','雪','雾'][_map_desc(w['desc'])]}, 气温{w['temp_c']}°C(体感{w['feels_c']}°C)")
    return "；".join(parts) if parts else ""


def _map_desc(desc):
    """天气描述 → 0-7 索引 (未知/晴/多云/阴/雨/大雨/雪/雾)"""
    d = str(desc)
    if any(k in d for k in ["晴", "Sunny", "Clear"]): return 1
    if any(k in d for k in ["多云", "Partly"]): return 2
    if any(k in d for k in ["阴", "Overcast"]): return 3
    if any(k in d for k in ["小雨", "Light rain", "Drizzle"]): return 4
    if any(k in d for k in ["大雨", "Heavy", "雨", "Rain"]): return 5
    if any(k in d for k in ["雪", "Snow"]): return 6
    if any(k in d for k in ["雾", "Mist", "Fog"]): return 7
    return 0


def selftest():
    print("=== 外部感知自测 ===")
    print("天气:", weather())
    print("搜索'量子计算':")
    for it in search("量子计算", 3):
        print(f"  - {it['title']}: {it['desc'][:60]}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
