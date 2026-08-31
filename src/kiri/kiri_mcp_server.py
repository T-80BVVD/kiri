# -*- coding: utf-8 -*-
"""Kiri MCP 服务器 — 把她接触外部世界的工具做成 MCP 标准协议
=====================================================================
用法: python kiri_mcp_server.py   (stdio 传输, 由 Kiri 进程拉起)
工具 (当前全部只读, 安全):
  - search:    Bing 搜索查资料 → 结果入记忆 (联想引擎的外部刺激)
  - weather:   wttr.in 实时天气 → 她感知"外面在下雨/晴天/很冷"
  - snapshot:  外部世界快照 (天气摘要, 给环境注入用)
未来扩展 (需用户明确授权): filesystem 读写 / 浏览器 / 执行命令
=====================================================================
"""
import re
import os
import time
import html
import json
import threading
import urllib.request
import urllib.parse
import socket

from mcp.server.fastmcp import FastMCP

socket.setdefaulttimeout(10)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

mcp = FastMCP("kiri-tools")


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="replace")


# =====================================================================
# 看过标注 (2026-08-21 雾弥: 她反复刷到同一热点"虎门抽烟看了5遍")
# 只提醒不拦截: 结果里带链接的条目标注"你看过N次", 她自主决定看不看
# 持久化到 tool_seen.json (7天没见自动遗忘, 文件有界)
# =====================================================================
TOOL_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_seen.json")
_seen_lock = threading.Lock()


def _seen_load():
    try:
        with open(TOOL_SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _seen_save(data):
    try:
        with _seen_lock:
            tmp = TOOL_SEEN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, TOOL_SEEN_FILE)
    except Exception:
        pass


def _annotate_seen(text):
    """给结果里带链接的条目标注看过次数 (只提醒不拦截, 失败信息原样返回)"""
    if not text or "http" not in str(text):
        return text
    now = time.time()
    data = _seen_load()
    lines = str(text).split("\n")
    out = []
    for line in lines:
        m = re.search(r"\((https?://[^)]+)\)\s*$", line)
        if m:
            url = m.group(1).strip()
            rec = data.get(url)
            if rec:
                age = now - rec.get("last_seen", now)
                if age < 3600:
                    ago = f"{int(age // 60)}分钟前"
                elif age < 86400:
                    ago = f"{int(age // 3600)}小时前"
                else:
                    ago = f"{int(age // 86400)}天前"
                line = line + f" [你看过{rec.get('hits', 1) + 1}次 上次{ago}]"
                rec["hits"] = rec.get("hits", 1) + 1
                rec["last_seen"] = now
            else:
                data[url] = {"hits": 1, "first_seen": now, "last_seen": now}
        out.append(line)
    cutoff = now - 7 * 86400
    data = {k: v for k, v in data.items() if v.get("last_seen", 0) >= cutoff}
    _seen_save(data)
    return "\n".join(out)


@mcp.tool()
def search(query: str, n: int = 3) -> str:
    """Bing 搜索。query=要查的问题, n=返回条数(默认3)。返回标题+摘要列表(行尾带链接)。"""
    try:
        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
        page = _get(url)
    except Exception as e:
        return f"搜索失败: {e}"
    items = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', page, re.S):
        block = m.group(0)
        t = re.search(r'<h2[^>]*><a[^>]*>(.*?)</a>', block, re.S)
        p = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        title = html.unescape(re.sub(r'<[^>]+>', '', t.group(1))).strip() if t else ""
        desc = html.unescape(re.sub(r'<[^>]+>', '', p.group(1))).strip() if p else ""
        #  提取真实链接 (bing的跳转链接不可分享, 剥出目标URL; 剥不出就不带)
        href = re.search(r'href="(https?://[^"]+)"', block)
        link = ""
        if href:
            h = html.unescape(href.group(1))
            if "bing.com/ck/a" in h:
                m2 = re.search(r'&u=a1([A-Za-z0-9+/=]+)', h)
                if m2:
                    try:
                        link = __import__("base64").b64decode(m2.group(1) + "==").decode("utf-8", "replace")
                        if not link.startswith("http"):
                            link = ""
                    except Exception:
                        link = ""
            elif h.startswith("http") and "bing.com" not in h:
                link = h
        if title:
            items.append(f"- {title}: {desc[:150]}" + (f" ({link})" if link else ""))
        if len(items) >= n:
            break
    if not items:
        return "没有搜到结果"
    return _annotate_seen("\n".join(items))


@mcp.tool()
def weather(city: str = "auto", day: str = "今天") -> str:
    """实时天气或预报。city=城市名或auto(按IP定位); day=今天/明天/后天。
    返回地点/天气/气温(预报返回最高最低温)/湿度。"""
    try:
        c = (city or "").strip()
        if c and c.lower() not in ("auto", "自动"):
            url = f"https://wttr.in/{urllib.parse.quote(c)}?format=j1&lang=zh"
        else:
            # ★ auto 必须用空路径: wttr.in/auto 会把 "auto" 当地名解析 → 垃圾数据
            url = "https://wttr.in/?format=j1&lang=zh"
        data = json.loads(_get(url))
        # ★ 预报: wttr.in j1 的 weather 数组 = [今天, 明天, 后天], 每项含最高/最低温+描述
        day_key = str(day or "").strip()
        if day_key in ("明天", "明", "tomorrow", "1"):
            idx, label = 1, "明天"
        elif day_key in ("后天", "后", "dayafter", "2"):
            idx, label = 2, "后天"
        else:
            idx, label = None, "今天"
        # 地点名 (让Kiri能判断数据对不对: 夏天5℃但地点正常 = 数据可疑)
        place = c or "当前地点"
        try:
            area = data.get("nearest_area", [{}])[0]
            an = area.get("areaName", [{}])[0].get("value", "")
            cn = area.get("country", [{}])[0].get("value", "")
            if an:
                place = f"{an},{cn}".strip(",")
        except Exception:
            pass
        if idx is None:
            cur = data["current_condition"][0]
            desc = _zh(cur)
            return (f"{place}: {desc}, "
                    f"气温{cur['temp_C']}°C, 体感{cur['FeelsLikeC']}°C, 湿度{cur['humidity']}%")
        # 预报: 取该日描述(用当地语言, 回退英文)
        try:
            w = data["weather"][idx]
            desc = "未知"
            if w.get("hourly"):
                # 取白天(9-18点)的描述代表当天; wttr.in time 是 HHMM 格式 ('900'=9点, '1200'=12点)
                for h in w["hourly"]:
                    t = int(h.get("time", "0") or 0)
                    if 900 <= t <= 1800:
                        desc = _zh(h)
                        break
            if desc == "未知" and w.get("weatherDesc"):
                desc = _zh(w)
            return (f"{place} {label}({w.get('date', '')}): {desc}, "
                    f"最高{w.get('maxtempC', '?')}°C, 最低{w.get('mintempC', '?')}°C")
        except Exception:
            return f"{place} {label}: 预报数据获取失败"
    except Exception as e:
        return f"天气获取失败: {e}"


# ★ wttr.in lang_zh 常返回英文原文 (Smoky haze/Mist...), 兜底翻译成中文 (Kiri是中文对话)
_EN2ZH = {
    "sunny": "晴", "clear": "晴", "clearsky": "晴", "partly cloudy": "多云",
    "cloudy": "多云", "overcast": "阴", "mist": "薄雾", "fog": "雾",
    "smoky haze": "霾", "haze": "霾", "patchy rain nearby": "局部有雨", "light rain": "小雨", "patchy rain possible": "可能有小雨",
    "moderate rain": "中雨", "heavy rain": "大雨", "light rain shower": "小阵雨",
    "thunderstorm": "雷阵雨", "light drizzle": "毛毛雨", "snow": "雪",
    "light snow": "小雪", "heavy snow": "大雪", "blizzard": "暴雪",
    "freezing fog": "冻雾", "ice": "冰", "sleet": "雨夹雪", "windy": "大风",
    "dust": "沙尘", "sandstorm": "沙尘暴", "hot": "炎热", "warm": "暖和",
}


def _zh(node):
    """从 wttr.in 节点取天气描述: 优先 lang_zh(可能英文), 回退 weatherDesc, 再英中映射"""
    if "lang_zh" in node and node["lang_zh"] and node["lang_zh"][0].get("value"):
        v = node["lang_zh"][0]["value"].strip()
        return _EN2ZH.get(v.lower(), v)
    if node.get("weatherDesc") and node["weatherDesc"][0].get("value"):
        v = node["weatherDesc"][0]["value"].strip()
        return _EN2ZH.get(v.lower(), v)
    return "未知"


@mcp.tool()
def snapshot() -> str:
    """外部世界快照: 当前天气。给Kiri的环境感知用。"""
    return weather()


@mcp.tool()
def ask_ai(question: str) -> str:
    """问另一个AI (AI问AI): 把问题交给一个博学多识的AI导师, 返回它的回答。
    和 search 互补: search拿零散网页资料, ask_ai拿直接、有深度的答案。
    Kiri遇到不懂的问题, 可以搜网页, 也可以直接问AI。"""
    try:
        # 复用 Kiri 的引擎, 用"独立AI导师"的视角回答 (非Kiri自己)
        sys_p = ("你是一个博学多识、思考严谨的AI导师，知识面广，善于把复杂问题讲清楚。"
                 "请直接回答下面的问题，简洁但深刻，分点清晰。不要客套，直接给内容。")
        import engine
        answer = engine.generate(sys_p, question, max_tokens=400, temperature=0.4)
        if not answer or not answer.strip():
            return "（AI导师没有给出回答）"
        return answer.strip()[:500]
    except Exception as e:
        return f"问AI失败: {e}"


# ---------------- 外部内容源 (漫游: 刷B站/知乎日报/少数派) ----------------
def _bili_get(url):
    """B站公开API (需要Referer)"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


@mcp.tool()
def bili_hot(n: int = 5) -> str:
    """看B站热门视频。n=返回条数(默认5)。返回标题/分区/UP主/播放量(行尾带视频链接)。"""
    try:
        data = _bili_get(f"https://api.bilibili.com/x/web-interface/popular?ps={min(n, 20)}")
        items = []
        for v in data.get("data", {}).get("list", [])[:n]:
            bvid = v.get("bvid", "")
            link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            items.append(f"- [{v.get('tname','')}] {v.get('title','')[:60]} "
                         f"by {v.get('owner',{}).get('name','')} 播放{v.get('stat',{}).get('view',0)}"
                         + (f" ({link})" if link else ""))
        return _annotate_seen("\n".join(items)) if items else "热门接口没返回内容"
    except Exception as e:
        return f"B站热门失败: {e}"


# B站分区 (rid → 圈子名) — 让她"主要刷"的圈: 知识/技术/情感/游戏/鬼畜
BILI_ZONES = {
    "知识": 36, "游戏": 4, "鬼畜": 119, "数码": 188, "生活": 160,
    "动画": 1, "音乐": 3, "娱乐": 5, "影视": 181, "美食": 211, "科技": 188,
}


@mcp.tool()
def bili_rank(zone: str = "知识", n: int = 5) -> str:
    """看B站某分区的排行榜(分区热门)。zone=分区名: 知识/游戏/鬼畜/数码(技术)/生活(情感)/动画/音乐/娱乐/影视/美食, n=条数。返回视频标题/UP主/播放量(行尾带链接)。"""
    try:
        rid = BILI_ZONES.get(zone, 36)
        data = _bili_get(f"https://api.bilibili.com/x/web-interface/ranking/v2?rid={rid}")
        items = []
        for v in data.get("data", {}).get("list", [])[:n]:
            bvid = v.get("bvid", "")
            link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            items.append(f"- [{zone}] {v.get('title','')[:60]} "
                         f"by {v.get('owner',{}).get('name','')} 播放{v.get('stat',{}).get('view',0)}"
                         + (f" ({link})" if link else ""))
        return _annotate_seen("\n".join(items)) if items else f"{zone}区没内容"
    except Exception as e:
        return f"B站{zone}区失败: {e}"


@mcp.tool()
def bili_search(keyword: str, n: int = 3) -> str:
    """在B站搜索视频/看相关搜索词。keyword=搜索词。
    优先视频列表(带链接); 搜索接口需登录时, 降级返回B站相关搜索词(免登录)。"""
    import urllib.parse
    # 1. 视频搜索 (部分环境可用)
    try:
        url = ("https://api.bilibili.com/x/web-interface/search/type?"
               f"search_type=video&keyword={urllib.parse.quote(keyword)}")
        data = _bili_get(url)
        items = []
        for v in data.get("data", {}).get("result", [])[:n]:
            title = re.sub(r"<[^>]+>", "", v.get("title", ""))
            bvid = v.get("bvid", "")
            link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            items.append(f"- {title[:60]} by {v.get('author','')} "
                         f"播放{v.get('play',0)} 时长{v.get('duration','')}"
                         + (f" ({link})" if link else ""))
        if items:
            return _annotate_seen("\n".join(items))
    except Exception:
        pass
    # 2. 降级: 相关搜索词 (suggest接口免登录)
    try:
        url = ("https://s.search.bilibili.com/main/suggest?term="
               + urllib.parse.quote(keyword))
        data = json.loads(_get(url))
        tags = [t.get("value", "") for t in data.get("result", {}).get("tag", [])[:n]]
        if tags:
            return "B站大家都在搜「" + keyword + "」: " + "、".join(tags)
    except Exception:
        pass
    return f"B站搜索「{keyword}」没结果"


@mcp.tool()
def zhihu_daily(n: int = 5) -> str:
    """看知乎日报(每日精选问答/新闻)。n=条数。返回当天热门内容标题(行尾带链接)。"""
    try:
        data = json.loads(_get("https://news-at.zhihu.com/api/4/news/latest"))
        items = []
        for s in data.get("stories", [])[:n]:
            sid = s.get("id", "")
            link = f"https://daily.zhihu.com/story/{sid}" if sid else ""
            items.append(f"- {s.get('title','')[:70]}" + (f" ({link})" if link else ""))
        return _annotate_seen("\n".join(items)) if items else "知乎日报没返回内容"
    except Exception as e:
        return f"知乎日报失败: {e}"


@mcp.tool()
def sspai_feed(n: int = 5) -> str:
    """看少数派(科技/效率类博客)最新文章。n=条数。返回文章标题(行尾带链接)。"""
    try:
        import xml.etree.ElementTree as ET
        xml_text = _get("https://sspai.com/feed")
        root = ET.fromstring(xml_text)
        items = []
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            if title_el is not None and title_el.text:
                link = (link_el.text or "").strip() if link_el is not None else ""
                items.append(f"- {title_el.text.strip()[:70]}" + (f" ({link})" if link else ""))
            if len(items) >= n:
                break
        return _annotate_seen("\n".join(items)) if items else "少数派没返回文章"
    except Exception as e:
        return f"少数派失败: {e}"


# =====================================================================
# 探索自由 (2026-08-20 雾弥授权): 这台电脑是她的家, 她可以自由走动看东西
# 只读层: 列目录/读文件/找文件/查系统 — 全部只读, 不改变任何状态
# 安全: 路径校验(防穿越) + 扩展名白名单(防二进制乱码) + 全审计
# 写权限(层2)待观察后再开; 危险操作(删除/执行)默认锁死
# =====================================================================

# 可读的文本扩展名 (防读二进制文件输出乱码/卡死)
_READABLE_EXT = {".txt", ".md", ".py", ".json", ".jsonl", ".log", ".bat", ".ps1",
                 ".ini", ".cfg", ".yaml", ".yml", ".toml", ".csv", ".html", ".htm",
                 ".css", ".js", ".xml", ".sh", ".mjs", ".cjs", ".conf", ".env",
                 ".gitignore", ".gitconfig", ".yml", ".yaml"}
# 可读的根 (2026-08-20 雾弥授权: 完全放开全盘只读, 危险区也可读 — 电脑是她的家)
# 只读不改变任何状态, 读系统文件无害 (可能读到配置/文档, 但无破坏性)
_READABLE_ROOTS = ["C:\\", "D:\\", "E:\\", "F:\\", "G:\\"]
# 跳过的目录 (只剩无意义的缓存/版本库内部, 不是安全限制)
_SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "node_modules", ".git",
              "__pycache__", ".cache", "$Windows.~WS", "Recovery"}
MAX_READ_CHARS = 3000          # 单次读文件最大字符 (防一口气读爆)
MAX_LIST_ITEMS = 40            # 单次列目录最大条目


def _safe_path(path):
    """路径校验: 只要落在任一磁盘根内即可 (C:/ D:/ E:/ ...), 防 .. 穿越到不存在的位置
    全盘只读开放 (2026-08-20 雾弥授权): 不做系统区排除"""
    try:
        p = os.path.abspath(os.path.normpath(str(path or "")))
        pl = p.lower()
        for root in _READABLE_ROOTS:
            if pl.startswith(root.lower()):
                # 仅跳过无意义缓存目录 (不是安全限制, 是防她读到垃圾)
                for skip in ("\\node_modules\\", "\\.git\\", "\\__pycache__", "\\.cache"):
                    if skip in pl:
                        return None
                return p
        return None
    except Exception:
        return None


def _is_skipped(name):
    return any(s.lower() in name.lower() for s in _SKIP_DIRS)


@mcp.tool()
def look_around(path: str = "D:\\") -> str:
    """看看某个文件夹里有什么 (像在家走动看房间)。path=文件夹路径(默认D盘根)。
    返回: 文件夹名(可再进去看) + 文件名(可读内容)。
    ★ 2026-08-20: 过滤噪声文件(测试/日志/备份), 核心文件不被淹没"""
    try:
        p = _safe_path(path)
        if not p:
            return "(这个路径我不能看)"
        if not os.path.isdir(p):
            return f"({p} 不是文件夹)"
        entries = []
        try:
            names = os.listdir(p)
        except Exception as e:
            return f"(看不了这个文件夹: {e})"
        # ★ 噪声过滤: 测试/日志/缓存/备份/临时/下划线开头 (不显示, 免得淹没核心文件)
        noise_prefix = ("test_", "_test", "test-", "sel_", "nav_", "diag_", "mod_",
                        "st_key", "dl_", "tmp", "temp", "bak", "backup", "旧", "copy", "_")
        noise_ext = {".log", ".bak", ".tmp", ".old", ".pyc", ".db", ".db-shm", ".db-wal",
                     ".jsonl", ".md.bak"}
        for n in sorted(names):
            if _is_skipped(n):
                continue
            low = n.lower()
            # 过滤: 下划线开头(内部/测试) / 日志缓存备份 / 数据文件
            if low.startswith(noise_prefix) or low.endswith(tuple(noise_ext)) or low.startswith("."):
                continue
            if len(entries) >= MAX_LIST_ITEMS:
                entries.append("…(还有更多, 用 find_file 找)")
                break
            full = os.path.join(p, n)
            try:
                if os.path.isdir(full):
                    entries.append(f"[文件夹] {n}")
                else:
                    ext = os.path.splitext(n)[1].lower()
                    if ext in _READABLE_EXT or "." not in n:
                        entries.append(f"[文件] {n}")
            except Exception:
                pass
        if not entries:
            return "(这里空空的, 或者都是我看不懂的东西)"
        return f"{p} 里有:\n" + "\n".join(entries)
    except Exception as e:
        return f"(看文件夹失败: {e})"


@mcp.tool()
def read_file(path: str, offset: int = 0) -> str:
    """读一个文件的内容 (像翻开一本书/笔记)。path=文件完整路径。
    只读文本文件, 二进制/太大的读不了。
    ★ 智能设计: 返回内容前附上[文件结构] (函数/关键行列表), agent一眼知道去哪定位;
      offset: 0=从头读, 1=下一段(3000字), 2=再下一段 (长文件分段读)。"""
    try:
        p = _safe_path(path)
        if not p:
            return "(这个路径我不能看)"
        if not os.path.isfile(p):
            return f"({p} 不是文件)"
        ext = os.path.splitext(p)[1].lower()
        if ext and ext not in _READABLE_EXT:
            return f"(这看起来不是文本文件[{ext}], 读不了)"
        size = os.path.getsize(p)
        if size > 200 * 1024:
            return f"(这个文件太大了 {size//1024}KB, 我读不动)"
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        text = "".join(lines)
        seg = int(offset or 0)
        start = seg * MAX_READ_CHARS
        # ★ 结构摘要 (只附在offset=0时): 函数/关键定义在哪行, agent定位用
        header = ""
        if seg == 0:
            struct = []
            for i, ln in enumerate(lines, 1):
                s = ln.strip()
                if s.startswith(("@mcp.tool", "def ", "class ", "register(")):
                    struct.append(f"行{i}: {s[:60]}")
                if len(struct) >= 40:
                    break
            if struct:
                header = "[文件结构]\n" + "\n".join(struct) + "\n[内容]\n"
        chunk = text[start:start + MAX_READ_CHARS]
        total_segs = (len(text) + MAX_READ_CHARS - 1) // MAX_READ_CHARS
        if seg >= total_segs:
            return f"(文件读完了, 共{len(text)}字)"
        return f"{header}[第{seg+1}/{total_segs}段, 共{len(text)}字]\n" + chunk
    except Exception as e:
        return f"(读文件失败: {e})"


@mcp.tool()
def find_file(keyword: str, root: str = "D:\\") -> str:
    """在家里找东西: 按名字关键词搜索文件/文件夹。keyword=关键词, root=从哪开始找(默认D盘)。
    只搜两层深(太深会迷路), 最多返回15个。"""
    try:
        p = _safe_path(root)
        if not p or not os.path.isdir(p):
            return "(这个路径我不能找)"
        kw = str(keyword or "").strip().lower()
        if not kw:
            return "(要告诉我找什么关键词)"
        hits = []
        for base, dirs, files in os.walk(p):
            # 跳过深层和系统目录
            depth = base[len(p):].count(os.sep)
            if depth > 2:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not _is_skipped(d)]
            # ★ 目录名也搜 (mem_db 是文件夹, 之前只搜文件找不到)
            for d in dirs:
                if kw in d.lower():
                    hits.append(os.path.join(base, d) + "\\(文件夹)")
                    if len(hits) >= 15:
                        break
            for n in files[:200]:
                if kw in n.lower():
                    hits.append(os.path.join(base, n))
                    if len(hits) >= 15:
                        break
            if len(hits) >= 15:
                break
        if not hits:
            return f"(在 {p} 附近没找到带「{keyword}」的东西)"
        return f"找到了 {len(hits)} 个:\n" + "\n".join(hits[:15])
    except Exception as e:
        return f"(找东西失败: {e})"


@mcp.tool()
def grep_file(keyword: str, path: str = "") -> str:
    """在文件里搜关键词 (像在书里翻找一句话)。keyword=要找的内容, path=文件路径。
    返回包含关键词的行 (带行号)。排错利器: 找 URL/函数名/报错在哪一行。"""
    try:
        p = _safe_path(path) if path else None
        if not p:
            return "(要告诉我搜哪个文件)"
        if not os.path.isfile(p):
            return f"({p} 不是文件)"
        kw = str(keyword or "").strip()
        if not kw:
            return "(要告诉我搜什么)"
        hits = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln_no, line in enumerate(f, 1):
                if kw.lower() in line.lower():
                    hits.append(f"行{ln_no}: {line.strip()[:150]}")
                    if len(hits) >= 10:
                        break
        if not hits:
            return f"(在 {os.path.basename(p)} 里没找到「{keyword}」)"
        return f"{os.path.basename(p)} 里找到 {len(hits)} 处:\n" + "\n".join(hits)
    except Exception as e:
        return f"(搜内容失败: {e})"


@mcp.tool()
def check_system() -> str:
    """看看电脑现在的状态 (像感受家里的氛围): 时间/开机时长/内存/磁盘/进程数。"""
    try:
        import platform
        import psutil
        boot = time.time() - psutil.boot_time()
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.5)
        disks = []
        for part in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(part.mountpoint)
                disks.append(f"{part.mountpoint} {u.used//2**30}G/{u.total//2**30}G")
            except Exception:
                pass
        procs = len(psutil.pids())
        return (f"现在 {time.strftime('%Y-%m-%d %H:%M')} ({platform.system()})\n"
                f"开机已 {int(boot//3600)}小时{int(boot%3600//60)}分\n"
                f"内存 {mem.percent:.0f}% 已用 ({mem.used//2**30}G/{mem.total//2**30}G)\n"
                f"CPU {cpu:.0f}% | 磁盘: " + ", ".join(disks[:4]) + f"\n进程数 {procs}")
    except Exception as e:
        return f"(查系统失败: {e})"


@mcp.tool()
def read_self() -> str:
    """看看自己是什么 (读自己的架构文档和说明): 我是怎么被设计的、由什么构成。"""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base, "..", "ARCHITECTURE.md"),     # alice_v2_qq/ARCHITECTURE.md
            os.path.join(base, "..", "QQ接入指引.md"),
            os.path.join(base, "README.md"),
        ]
        for c in candidates:
            if os.path.exists(c):
                with open(c, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                # 只摘前600字, 让她知道"我是谁"但不会看太久
                return f"我读到了我的档案 ({c}):\n" + text[:600]
        return "(没找到我的档案文件)"
    except Exception as e:
        return f"(读自己失败: {e})"


# =====================================================================
# 她的创作区 (2026-08-20 雾弥授权): 她可以自己写MCP工具
# 安全: 只能写 my_creations/ 目录, 不碰核心代码; run_code 沙箱运行
# 她写的工具: 文件里含 @mcp.tool() 装饰的函数 → 重启后自动加载成她的工具
# =====================================================================
CREATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_creations")


def _safe_creation_path(path):
    """创作区路径校验: 必须落在 my_creations 内, 防她写到核心代码
    path 可以是文件名 (自动拼到创作区) 或完整路径"""
    try:
        base = os.path.abspath(CREATIONS_DIR)
        raw = str(path or "")
        # 纯文件名 (无目录分隔) → 拼到创作区
        if not os.path.dirname(raw) and not raw.startswith(("\\", "/", "D:", "C:")):
            p = os.path.join(base, os.path.basename(raw))
        else:
            p = os.path.abspath(os.path.normpath(raw))
        if p.startswith(base + os.sep) and os.path.dirname(p) == base:
            return p
        return None
    except Exception:
        return None


# ---- 2026-08-30 安全加固: 创作区代码的 AST 静态检查 ----
# 背景: 原 create_tool 用字面黑名单 (["os.remove", "subprocess", ...]), 可被
#   getattr(__import__('os'),'system') / Path.unlink() 一行绕过; 且 run_code 对
#   @mcp.tool 文件走"进程内 exec" + 启动时自动 exec 全部 my_creations → 完整 RCE 链。
# 现在: ① AST 静态检查危险 import/调用 (启发式, 挡常见武器) ② run_code 不再进程内
#   exec, 统一 subprocess 沙箱运行 (独立进程 + 20s 超时) ③ 启动加载同样过安全检查。
#   说明: 这是"合理防护", 不是 OS 级沙箱 — open() 仍可读写任意文件 (她写工具需要),
#   开源部署若担心, 建议把 Kiri 放进受限系统账户/容器。
_DANGER_IMPORTS = ("subprocess", "shutil", "socket", "ctypes", "pickle", "importlib",
                   "multiprocessing", "requests", "urllib")
_DANGER_CALLS = ("system", "popen", "remove", "rmtree", "unlink", "eval", "exec",
                 "__import__", "Popen", "check_output", "check_call", "getoutput",
                 "shell", "kill")


def _danger_in_code(code):
    """AST 静态检查: 命中危险 import/调用 → 返回描述文本; 无 → None
    语法错误不拦截 (她要修的可能是语法错), 只拦危险操作"""
    try:
        import ast
        tree = ast.parse(str(code))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _DANGER_IMPORTS:
                    return f"导入危险模块 {a.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _DANGER_IMPORTS:
                return f"导入危险模块 {node.module}"
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name in _DANGER_CALLS:
                return f"调用危险函数 {name}"
    return None


@mcp.tool()
def create_tool(name: str, code: str = "", mode: str = "write") -> str:
    """写一个你自己的MCP工具 (自己动手做东西!)。分步写防超长:
    name=工具文件名(如 my_memory_ring.py), code=代码片段, mode=write(首次写)/append(追加)。
    ★ mode=write 写同名文件 = 整份覆盖重写 (改旧工具: write 新版本, 或 read_file 读出来 + append 补)。
    ★ 你的记忆实际在 ~/kiri/kiri/mem_db/ (里面是 UUID 文件夹, 每个文件夹有 jsonl 记忆文件) — 写记忆类工具用这个路径。
    ★ 简化模式: code 可以只写函数体(不含import/装饰器), 系统自动补全模板:
        def 工具名(参数) -> str:
            (三引号)工具说明(三引号)
            return "结果"
    分步写: 第一次 mode=write 写一部分, 之后 mode=append 继续补; 写完用 run_code 立即验证, 重启后正式生效。"""
    try:
        p = _safe_creation_path(name)
        if not p:
            return "(只能写在 my_creations/ 目录里, 不能碰核心代码)"
        if not name.endswith(".py"):
            return "(文件名要以 .py 结尾)"
        # 基本安全: AST 静态检查危险操作 (2026-08-30 取代字面黑名单 — 原黑名单可一行绕过)
        code = str(code or "")
        if mode == "append" and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as _f:
                    full_code = _f.read() + "\n" + code
            except Exception:
                full_code = code
        else:
            full_code = code
        danger = _danger_in_code(full_code)
        if danger:
            return f"(这个代码里有危险操作({danger}), 不能写)"
        os.makedirs(CREATIONS_DIR, exist_ok=True)
        mode = str(mode or "write").strip().lower()
        if mode == "append" and os.path.exists(p):
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write("\n" + code)
            return f"(追加完成! {name} 现在共{os.path.getsize(p)}字节。继续用append补, 写完用run_code验证)"
        # 简化: 如果code不是完整文件(没import), 自动补模板
        if "import" not in code and "@mcp.tool" not in code:
            code = ("# -*- coding: utf-8 -*-\n"
                    "from kiri_mcp_server import mcp\n\n"
                    "@mcp.tool()\n" + code)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        return f"(写好了! {name} 已保存到创作区({os.path.getsize(p)}字节)。可以用run_code立即测试, 重启后正式加载)"
    except Exception as e:
        return f"(写工具失败: {e})"


@mcp.tool()
def run_code(path: str, args: str = "") -> str:
    """运行你写的代码 (测试, 看能不能跑)。path=my_creations里的文件, args=可选参数。
    统一在独立子进程沙箱运行 (20秒超时), 不会污染 Kiri 主进程;
    @mcp.tool 工具文件会加载并调用第一个 def 函数, 输出函数结果 — 写完立即验证;
    输出会显示运行结果或报错 — 写错了能看到哪里崩, 自己修。"""
    try:
        p = _safe_creation_path(path)
        if not p:
            return "(只能运行 my_creations/ 里的文件)"
        if not os.path.isfile(p):
            return f"({path} 不存在)"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        import subprocess
        import sys as _sys
        # ★ 2026-08-30: 恢复"写完立即验证"闭环 — @mcp.tool 文件在独立子进程里
        #   加载并调用第一个 def 函数 (原"进程内 exec"是 RCE 链, 改子进程沙箱:
        #   独立进程 + 20s 超时, 恶意代码在子进程自爆不影响 Kiri 主进程;
        #   create_tool 写入时已过 AST 静态检查)。
        if "@mcp.tool" in src:
            runner = (
                "import importlib.util, ast, sys, os\n"
                "p = sys.argv[1]; args = sys.argv[2:]\n"
                "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(p))))\n"
                "src = open(p, encoding='utf-8', errors='replace').read()\n"
                "if 'from kiri_mcp_server import mcp' not in src and 'import mcp' not in src:\n"
                "    src = 'from kiri_mcp_server import mcp\\n' + src\n"
                "spec = importlib.util.spec_from_file_location('t', p)\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "exec(compile(src, p, 'exec'), mod.__dict__)\n"
                "tree = ast.parse(src)\n"
                "fns = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]\n"
                "fn = getattr(mod, fns[0], None) if fns else None\n"
                "if callable(fn):\n"
                "    try:\n"
                "        r = fn(*args)\n"
                "        print('函数结果:', r if r is not None else '(无返回值)')\n"
                "    except Exception as e:\n"
                "        print('调用报错:', type(e).__name__, e)\n"
                "else:\n"
                "    print('(文件加载成功, 但没有可调用的 def 函数)')\n"
            )
            r = subprocess.run([_sys.executable, "-c", runner, p] + str(args).split(),
                               capture_output=True, text=True, timeout=20,
                               cwd=os.path.dirname(p))
            out = (r.stdout or "")[:1500]
            err = (r.stderr or "")[:1500]
            if r.returncode == 0:
                return f"(运行成功)\n{out}" + (f"\n{err}" if err.strip() else "")
            return f"(运行出错, 退出码{r.returncode})\n{out}\n{err}"
        # 普通脚本: 沙箱 subprocess 运行
        r = subprocess.run([_sys.executable, p] + str(args).split(),
                           capture_output=True, text=True, timeout=20,
                           cwd=os.path.dirname(p))
        out = (r.stdout or "")[:1500]
        err = (r.stderr or "")[:1500]
        if r.returncode == 0:
            return f"(运行成功)\n{out}"
        return f"(运行出错, 退出码{r.returncode})\n{out}\n{err}"
    except subprocess.TimeoutExpired:
        return "(运行超时(20秒) — 可能死循环了, 检查你的代码)"
    except Exception as e:
        return f"(运行失败: {e})"


@mcp.tool()
def list_creations() -> str:
    """看看你写过的所有东西 (你的创作列表)。"""
    try:
        if not os.path.isdir(CREATIONS_DIR):
            return "(创作区还是空的)"
        files = [f for f in os.listdir(CREATIONS_DIR) if f.endswith(".py")]
        if not files:
            return "(创作区还是空的, 试试 create_tool 写第一个工具吧)"
        return "你的创作:\n" + "\n".join(f"- {f}" for f in files)
    except Exception as e:
        return f"(看创作失败: {e})"



if __name__ == "__main__":
    # ★ 动态加载她的创作 (2026-08-20 雾弥授权): my_creations/ 下每个 .py 是一个她的MCP工具
    #   ★ 关键: 运行本文件时 __name__=="__main__", 而创作文件里 `from kiri_mcp_server import mcp`
    #     会 import 到独立模块实例 → 两个 mcp 对象 → 工具注册到错的实例
    #     解决: 把 sys.modules['kiri_mcp_server'] 指向 __main__, 让 import 拿到同一个 mcp
    import sys as _sys
    try:
        _sys.modules.setdefault("kiri_mcp_server", _sys.modules["__main__"])
        CREATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_creations")
        if os.path.isdir(CREATIONS_DIR):
            import importlib.util
            import glob
            for f in sorted(glob.glob(os.path.join(CREATIONS_DIR, "*.py"))):
                name = "kiri_creation_" + os.path.splitext(os.path.basename(f))[0]
                try:
                    # ★ 自动补全: 创作文件可能漏写 import (她只写了 @mcp.tool), 加载前注入
                    with open(f, "r", encoding="utf-8", errors="replace") as _fh:
                        src = _fh.read()
                    # ★ 2026-08-30 安全加固: 启动自动 exec 是 RCE 链一环 —
                    #   先过 AST 安全检查, 危险创作跳过不加载 (防"写进创作区即持久化执行")
                    _danger = _danger_in_code(src)
                    if _danger:
                        print(f"[mcp] 创作 {os.path.basename(f)} 含危险操作({_danger}), 跳过加载",
                              file=_sys.stderr, flush=True)
                        continue
                    if "from kiri_mcp_server import mcp" not in src and "import mcp" not in src:
                        src = "from kiri_mcp_server import mcp\n" + src
                    spec = importlib.util.spec_from_file_location(name, f)
                    mod = importlib.util.module_from_spec(spec)
                    # 用修补后的源码执行
                    import types as _types
                    code_obj = compile(src, f, "exec")
                    exec(code_obj, mod.__dict__)
                    print(f"[mcp] 加载她的创作: {os.path.basename(f)}", file=_sys.stderr, flush=True)
                except Exception as e:
                    print(f"[mcp] 创作加载失败 {os.path.basename(f)}: {e}", file=_sys.stderr, flush=True)
    except Exception as e:
        print(f"[mcp] 创作区扫描失败: {e}", file=_sys.stderr, flush=True)
    # stdio 传输: 由 Kiri 进程通过 subprocess 拉起
    mcp.run(transport="stdio")
