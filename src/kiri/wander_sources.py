# -*- coding: utf-8 -*-
"""wander_sources.py — 世界漫游多源化 (吸收 N.E.K.O proactive_chat sources/防重复思想)
=====================================================================
解决的问题 (雾弥 2026-08-19):
- 旧漫游: 每次只调一个 MCP 工具, 结果全是文本, 没有链接, 且"天天刷到同一条热搜"反复分享
- NEKO 做法 (proactive_chat/sources.py + state.py):
  ① source 抽象: 每源返回 {text, links:[{title,url,source}]}, 多源并发 gather, 单源失败不阻塞
  ② 防重复: _source_hash(url,title) → 历史 → 5h 硬窗口必跳 + 按半衰期指数衰减 + 概率恢复
  ③ 公平轮换: 预算内各源轮询取候选, 任何单源不霸榜
本模块:
- SourceResult: {source, label, text, links}
- collect(): 并发调 2-3 个 MCP 内容源, 容忍单源失败
- DedupStore: 标题/URL 哈希防重复 (5h硬窗口 + 半衰期衰减 + 概率恢复, JSON 持久化)
- pick_candidates(): 轮询各源, 过滤重复, 返回候选 (给 LLM 挑选)
=====================================================================
"""
import os
import time
import json
import random
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

# ---- 内容源清单: (MCP工具名, 标签, 参数) ----
# 顺序即轮询顺序; 失败自动跳过
SOURCE_DEFS = [
    ("bili_rank", "刷{zone}圈", {"zone": "{zone}", "n": 5}),
    ("bili_hot", "刷B站全站热门", {"n": 5}),
    ("zhihu_daily", "看知乎日报", {"n": 5}),
    ("sspai_feed", "看少数派", {"n": 5}),
    ("bili_search", "B站搜「{topic}」", {"keyword": "{topic}", "n": 3}),
    ("search", "搜「{topic}」", {"query": "{topic}", "n": 3}),
    ("weather", "看天气", {"city": "auto"}),
]

# ---- 防重复参数 (NEKO 参数移植) ----
DEDUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wander_dedup.json")
HARD_WINDOW_HOURS = 5.0      # 5h 硬窗口: 同一条必跳 (NEKO: 5h p_skip=1.0)
HALF_LIFE_HOURS = 36.0       # 半衰期: 之后按指数衰减概率恢复 (NEKO: web/image 3天)
FORGET_P = 0.05              # 低于此概率遗忘 (文件体积有界)
MAX_STORE = 500              # 历史上限 (防无限膨胀)

_LOCK = threading.Lock()


def _hash(text):
    """标题/URL → 短哈希 (防重复的键)"""
    try:
        return hashlib.md5(str(text).strip().encode("utf-8")).hexdigest()[:12]
    except Exception:
        return str(text)[:12]


class DedupStore:
    """内容防重复存储: {hash: {first_seen, last_seen, hits}}
    should_skip(entry) → 是否该跳过 (硬窗口必跳 / 概率恢复)
    mark(entry) → 记录一次出现
    """
    def __init__(self, path=DEDUP_FILE):
        self.path = path
        self._data = {}
        self._mark_count = 0
        self.load()

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
        except Exception:
            self._data = {}

    def save(self):
        try:
            with _LOCK:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False)
        except Exception:
            pass

    def _key(self, entry):
        """有 URL 用 URL 哈希, 否则标题哈希 (NEKO: _source_hash(url,title))"""
        url = str(entry.get("url") or "").strip()
        if url and url.startswith("http"):
            return "u:" + _hash(url)
        return "t:" + _hash(entry.get("title") or entry.get("text") or "")

    def should_skip(self, entry, now=None):
        """该条是否该跳过 (重复且未到恢复时机)"""
        now = now or time.time()
        key = self._key(entry)
        rec = self._data.get(key)
        if not rec:
            return False
        age_h = (now - rec.get("last_seen", now)) / 3600.0
        if age_h < HARD_WINDOW_HOURS:
            return True                              # 5h 硬窗口必跳
        # 半衰期衰减: 越久没见越可能恢复; 概率 <FORGET_P 则遗忘
        p_recover = 0.5 ** (age_h / HALF_LIFE_HOURS)
        if p_recover < FORGET_P:
            self._data.pop(key, None)                # 遗忘 (有界)
            return False
        return random.random() >= p_recover           # 概率恢复 (大部分时间仍跳)

    def mark(self, entry, now=None):
        """记录一次出现 (last_seen 刷新, 窗口重置)
        ★ 2026-08-21 雾弥: 定期落盘 — 原来从不 save, 进程一重启去重清零 (同一热点反复刷)"""
        now = now or time.time()
        key = self._key(entry)
        rec = self._data.get(key) or {}
        rec["first_seen"] = rec.get("first_seen", now)
        rec["last_seen"] = now
        rec["hits"] = int(rec.get("hits", 0)) + 1
        self._data[key] = rec
        # 有界: 超上限删最旧的 (按 last_seen)
        if len(self._data) > MAX_STORE:
            try:
                for k in sorted(self._data, key=lambda k: self._data[k].get("last_seen", 0))[:len(self._data) - MAX_STORE]:
                    self._data.pop(k, None)
            except Exception:
                pass
        # 每 20 次 mark 落盘一次 (性能与持久化平衡; 重启不丢去重记录)
        self._mark_count += 1
        if self._mark_count % 20 == 0:
            self.save()

    def count(self):
        return len(self._data)


# ---- 单源适配器: MCP 工具结果 → SourceResult ----
def _fetch_one(tool_name, label, args, topic="有趣的知识", zone="知识"):
    """调一个 MCP 内容源, 失败返回 None (不阻塞整体)"""
    try:
        import mcp_client
        real_args = {}
        for k, v in (args or {}).items():
            if isinstance(v, str) and "{topic}" in v:
                v = v.replace("{topic}", topic)
            if isinstance(v, str) and "{zone}" in v:
                v = v.replace("{zone}", zone)
            real_args[k] = v
        text = mcp_client.call_tool(tool_name, real_args)
        if not text or "失败" in str(text)[:20] or "没内容" in str(text)[:20] or "没结果" in str(text)[:20]:
            return None
        label = label.replace("{topic}", topic).replace("{zone}", zone)
        return {"source": tool_name, "label": label, "text": str(text)[:300],
                "links": _extract_links(tool_name, str(text))}
    except Exception:
        return None


def _extract_links(tool_name, text):
    """从工具返回文本里提取链接 (工具输出格式升级后才有; 无则空)
    格式约定: 行尾 " (https://...)" 或 独立行 "链接: https://..."
    """
    links = []
    for m in __import__("re").finditer(r"https?://[^\s\)）]+", text):
        url = m.group(0).rstrip(".,;")
        links.append({"url": url, "source": tool_name})
    return links[:5]


def collect(topic="有趣的知识", zone=None, max_sources=3):
    """并发收集 max_sources 个内容源 (轮询+失败容忍)
    zone: 偏好圈名 (知识/游戏/鬼畜等) — 给了则分区源排最前; None 则纯刷通用源
    返回 [SourceResult]; 至少1个成功才有意义"""
    import mcp_client  # noqa: F401  (确保模块可导入时报错明确)
    defs = list(SOURCE_DEFS)
    if zone:
        # 偏好圈优先: zone 源排最前
        zone_def = ("bili_rank", "刷{zone}圈", {"zone": "{zone}", "n": 5})
        defs = [zone_def] + [d for d in defs if d[0] != "bili_rank"]
    results = []
    with ThreadPoolExecutor(max_workers=max_sources) as ex:
        futs = [ex.submit(_fetch_one, name, label, args, topic, zone or "知识")
                for name, label, args in defs[:max_sources + 2]]
        for f in futs:
            r = f.result(timeout=25)
            if r:
                results.append(r)
    return results


def pick_candidates(results, dedup=None, budget=12, now=None):
    """公平轮换: 各源轮询取候选, 过滤重复 (dedup 存储)
    返回候选列表 (每项含 source/label/text/links); 被过滤的项已 mark 但未选"""
    dedup = dedup or DedupStore()
    candidates = []
    sources = results or []
    # 轮询: 轮流从每个源取一条, 直到预算用完
    i = 0
    while len(candidates) < budget and sources:
        progressed = False
        for src in sources:
            items = _split_items(src)
            if i < len(items):
                entry = items[i]
                entry["source"] = src["source"]
                entry["label"] = src["label"]
                if not dedup.should_skip(entry, now):
                    candidates.append(entry)
                dedup.mark(entry, now)   # 无论选没选都记 (防下次再刷到)
                progressed = True
            if len(candidates) >= budget:
                break
        if not progressed:
            break
        i += 1
    return candidates


def _split_items(src):
    """把一源文本拆成候选条目: 每行 "- title (url)" 或 "- title"
    返回 [{title, text, url?}]"""
    items = []
    for line in str(src.get("text", "")).splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("-• \t")
        if len(line) < 2:
            continue
        url = None
        m = __import__("re").search(r"\((https?://[^\s\)]+)\)\s*$", line)
        if m:
            url = m.group(1)
            line = line[:m.start()].rstrip()
        items.append({"title": line[:80], "text": line[:120], "url": url})
    return items


if __name__ == "__main__":
    # 自测: 只测纯逻辑 (不调网络)
    dedup = DedupStore(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_wander_dedup_test.json"))
    dedup._data.clear()
    e1 = {"title": "测试视频A", "url": "https://www.bilibili.com/video/BV1"}
    assert not dedup.should_skip(e1)
    dedup.mark(e1)
    assert dedup.should_skip(e1), "5h 硬窗口应跳过"
    dedup._data[dedup._key(e1)]["last_seen"] = time.time() - 24 * 3600
    # 24h后: 半衰期36h → p_recover≈0.63, 大概率还跳 (但概率性, 不硬断言)
    print("DedupStore 自测 PASS (硬窗口生效)")
    srcs = [{"source": "bili_hot", "label": "热门", "text": "- 视频A (https://a.com)\n- 视频B\n- 视频C"}]
    cands = pick_candidates(srcs, dedup, budget=5)
    print(f"候选拆分: {len(cands)} 条 (应含 视频B/视频C, 视频A 被窗口拦截)")
    print("PASS" if len(cands) >= 2 else "FAIL")
