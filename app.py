# -*- coding: utf-8 -*-
"""Kiri 对外实验版 (QQ 版) — 无 TTS/视觉, 只保留: 后台监控 + 回溯界面
=====================================================================
设计取舍 (2026-08-17 雾弥指令):
  - 砍 TTS/STT/VTube: QQ 通道不需要语音, 砍掉能去掉 CosyVoice 加载(启动慢/易挂)
  - 砍主对话窗口: 对话走 QQ, 本地不需要聊天面板
  - 保留: 后台监控窗口(思考链+工具调用, 应急排障) + 回溯界面(完整时间线, 事后翻阅)
  - 核心: kiri 类 (多用户: 每用户独立记忆库 + 人格分流)
通道:
  - JS API: send_message_to(text, user) — 前端/QQ桥都用它
  - QQ 桥: qq_bridge.py 直接调 app.kiri.respond(text, user=user) 并回发
数据: events.jsonl + history.jsonl + proactives.jsonl + kiri_mind.jsonl (与dev互通格式)
用法: python app.py            # 真实模式 (需 QQ 配置或走监控面板调试输入)
      python app.py --demo     # 零依赖演示 (内置虚构示例, 无需 key/QQ/数据库)
=====================================================================
"""
import os
import sys
import json
import time
import threading

# 核心模块统一放在 src/kiri/ 下 (轻量分层: 模块内部平铺 import 不变, 靠 sys.path 指向 src/kiri)
BASE = os.path.dirname(os.path.abspath(__file__))
KIRI_DIR = os.path.join(BASE, "src", "kiri")
sys.path.insert(0, KIRI_DIR)
# 为 src/kiri 也是包, 加 package 根
sys.path.insert(0, os.path.join(BASE, "src"))

# ★ 零依赖演示模式: 不加载 kiri/配置/凭据, 直接转发 demo.py (2026-08-29)
if "--demo" in sys.argv:
    import demo
    sys.exit(demo.main())

import config
import kiri as kiri_mod

import webview
import pystray
from PIL import Image, ImageDraw

# 数据/界面统一定位到 src/kiri (与核心模块同处, 运行数据集中于此)
_BASE_ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(_BASE_ROOT, "src", "kiri")
HISTORY_FILE = os.path.join(BASE, "history.jsonl")
PROACTIVES_FILE = os.path.join(BASE, "proactives.jsonl")
MIND_FILE = os.path.join(BASE, "kiri_mind.jsonl")
UI_DIR = os.path.join(BASE, "ui")

# ---- 全局 ----
kiri = kiri_mod.Kiri()
_MAIN_HISTORY = []
PROACTIVES = []
_STOPPED = False
_windows = {}       # name → window


def _rebuild_from_events():
    """从 events.jsonl 重建聊天历史 (respond→对话对, proactive→[主动])"""
    hist = []
    ev_path = kiri_mod.EVENTS_FILE
    if not os.path.exists(ev_path):
        return hist
    items = []
    with open(ev_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = e.get("kind")
            if k == "respond" and e.get("user"):
                items.append((e.get("ts", ""), "user", e["user"]))
                if e.get("reply"):
                    items.append((e.get("ts", ""), "assistant", e["reply"]))
            elif k == "proactive" and e.get("say") and e.get("said"):
                items.append((e.get("ts", ""), "assistant", f"[主动] {e['say']}"))
    items.sort(key=lambda x: x[0])
    for ts, role, content in items:
        hist.append({"role": role, "content": content, "ts": ts})
    return hist


def _load_history():
    """聊天历史: 优先读 history.jsonl; 空则从 events.jsonl 重建并落盘"""
    hist = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    hist.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if hist:
            return hist
    hist = _rebuild_from_events()
    if hist:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            for m in hist:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return hist


def _append_history(msg):
    """只更新内存历史 (文件由 kiri 核心统一写 history.jsonl, 避免重复)"""
    if "ts" not in msg:
        msg["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _MAIN_HISTORY.append(msg)


def _load_proactives():
    ps = []
    if os.path.exists(PROACTIVES_FILE):
        with open(PROACTIVES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ps.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return ps


def _append_proactive(say, reason):
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "say": say, "reason": reason}
    PROACTIVES.append(rec)
    with open(PROACTIVES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_MAIN_HISTORY = _load_history()
PROACTIVES = _load_proactives()


def _on_proactive(say, reason):
    """主动发言: 记日志 (对外版无TTS, 声音交给QQ通道/静默)"""
    _append_proactive(say, reason)
    _append_history({"role": "assistant", "content": f"[主动] {say}"})
    # ★ 网页版"她主动说"推送 (M3 前置, 2026-08-27)
    try:
        import monitor_server
        monitor_server.push_proactive(say, reason)
    except Exception:
        pass


kiri.on_proactive = _on_proactive

# ---- QQ 桥 (官方 API 优先; 无配置回退 NapCat; 都未配置则静默) ----
_qq_bridge = None
try:
    # ★ 官方 API 版 (2026-08-27): 不会被踢号, 国内直连, 被动模式天然成立
    import qq_bridge_official
    _qq_cfg = qq_bridge_official.load_config()
    if _qq_cfg.get("app_id") and _qq_cfg.get("client_secret"):
        _qq_bridge = qq_bridge_official.start(
            _qq_cfg,
            lambda text, user, scene="private", group_context=None:
                kiri.respond(text, user=user, scene=scene, group_context=group_context),
            kiri=kiri)

        def _on_proactive_qq(say, reason):
            _on_proactive(say, reason)
            if _qq_bridge is not None:
                try:
                    _qq_bridge.enqueue_proactive(say)
                except Exception:
                    pass
        kiri.on_proactive = _on_proactive_qq
        print(f"[app] QQ官方桥已启动 (AppId={_qq_cfg.get('app_id')})")
    else:
        # 回退: NapCat 版 (旧方案, 配置里有 owner_qq 才启动)
        import qq_bridge
        _qq_cfg2 = qq_bridge.load_config()
        if _qq_cfg2.get("owner_qq"):
            _qq_bridge = qq_bridge.start(
                _qq_cfg2,
                lambda text, user, scene="private", group_context=None:
                    kiri.respond(text, user=user, scene=scene, group_context=group_context),
                kiri=kiri)

            def _on_proactive_qq2(say, reason):
                _on_proactive(say, reason)
                if _qq_bridge is not None:
                    try:
                        _qq_bridge.enqueue_proactive(say)
                    except Exception:
                        pass
            kiri.on_proactive = _on_proactive_qq2
            print(f"[app] QQ桥(NapCat)已启动 (owner_qq={_qq_cfg2.get('owner_qq')})")
        else:
            print("[app] QQ桥未启动: 官方配置缺 app_id/secret, NapCat 配置缺 owner_qq")
except Exception as exc:
    print(f"[app] QQ桥启动失败: {exc}")


def _status_dict():
    try:
        e = kiri.state.emotion.state
        mood = e["deep_affect"]["current_mood"]
        plea = e["surface_emotion"]["pleasure"]
    except Exception:
        mood = plea = 0.0
    return {
        "mood": round(float(mood), 3),
        "pleasure": round(float(plea), 3),
        "boredom": round(float(kiri.state.boredom), 3),
        "silence_min": int((time.time() - kiri.state.last_interact) / 60),
        "budget_left": max(0, config.PROACTIVE_BUDGET_PER_DAY - kiri.state.used_today),
        "memories": 0,
        "stopped": kiri.state.stopped,
    }


def _last_logs(n=15):
    rows = []
    try:
        with open(kiri_mod.EVENTS_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        for ln in lines[-n:]:
            try:
                ev = json.loads(ln)
                rows.append({"ts": ev.get("ts", ""), "kind": ev.get("kind", ""),
                             "text": (ev.get("user") or ev.get("say") or "")[:40],
                             "latency": str(ev.get("latency", ""))[:6]})
            except json.JSONDecodeError:
                pass
    except OSError:
        rows = []
    return rows[::-1]


def _memories():
    try:
        data = kiri.memory.collection.get(limit=20)
        rows = []
        for doc, meta in zip(data["documents"], data["metadatas"]):
            meta = meta or {}
            rows.append({"text": doc[:60], "session": meta.get("session", "-"),
                         "ts": time.strftime("%m-%d %H:%M",
                                             time.localtime(float(meta.get("timestamp", 0))))})
        return rows
    except Exception:
        return [{"text": "读取失败", "session": "-", "ts": "-"}]


# ---- JS API ----
BRIDGE_LOG = os.path.join(BASE, "api_bridge.log")


def _bridge_log(msg):
    try:
        with open(BRIDGE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


class Api:
    def ping(self):
        return "pong"

    def _safe(self, fn, default=None):
        try:
            return fn()
        except Exception as exc:
            return {"error": str(exc), "result": default}

    def send_message(self, text):
        return self.send_message_to(text, "雾弥")

    def send_message_to(self, text, user):
        """多用户对话入口 (前端/QQ桥共用)"""
        def do():
            _bridge_log(f"send_message_to text={text[:20]!r} user={user}")
            if not text or not text.strip():
                return {"reply": "", "history": list(_MAIN_HISTORY)}
            try:
                reply = kiri.respond(text, user=user)
            except Exception as exc:
                reply = f"(出错了: {exc})"
            _append_history({"role": "user", "content": text, "user": user})
            _append_history({"role": "assistant", "content": reply, "user": user})
            return {"reply": reply, "history": list(_MAIN_HISTORY)}
        return self._safe(do)

    def get_poll(self):
        def do():
            return {
                "status": _status_dict(),
                "proactives": list(PROACTIVES[-10:][::-1]),
                "logs": _last_logs(),
                "history": list(_MAIN_HISTORY),
                "memories": _memories(),
            }
        return self._safe(do)

    def get_history(self):
        return self._safe(lambda: list(_MAIN_HISTORY))

    def get_monitor(self):
        """监控窗口: 思考链(reverie/curiosity/thought) + 工具调用 (应急排障)"""
        def do():
            chain = []
            tools = []
            try:
                with open(kiri_mod.EVENTS_FILE, encoding="utf-8") as f:
                    lines = f.readlines()
                for ln in lines[-120:]:
                    try:
                        ev = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    k = ev.get("kind")
                    if k in ("reverie", "curiosity", "thought"):
                        chain.append({
                            "ts": ev.get("ts", ""),
                            "kind": k,
                            "text": (ev.get("thought") or ev.get("question") or "")[:90],
                            "memory": (ev.get("memory") or "")[:40],
                            "salience": ev.get("salience"),
                            "result": (ev.get("result") or "")[:80],
                            "round": ev.get("round"),
                            "verdict": ev.get("verdict", ""),
                            "user": ev.get("user", ""),
                        })
                    elif k == "tool_call":
                        tools.append({
                            "ts": ev.get("ts", ""),
                            "tool": ev.get("tool", ""),
                            "args": ev.get("args", ""),
                            "result": (ev.get("result") or "")[:100],
                            "ok": ev.get("ok", False),
                            "latency": ev.get("latency", 0),
                        })
            except OSError:
                pass
            try:
                import mcp_client
                mem_tools = mcp_client.get_tool_calls(10)
                for t in mem_tools:
                    if all(t["ts"] != x.get("ts") or t["tool"] != x.get("tool") for x in tools):
                        tools.append(t)
            except Exception:
                pass
            return {"chain": chain[-40:][::-1], "tools": tools[-30:][::-1]}
        return self._safe(do)

    def get_trace(self, n=300):
        """回溯界面: kiri_mind.jsonl 完整时间线 (对话+念头+工具+主动, 事后翻阅)"""
        def do():
            rows = []
            try:
                with open(MIND_FILE, encoding="utf-8") as f:
                    lines = f.readlines()
                for ln in lines[-n:]:
                    try:
                        ev = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    rows.append({
                        "ts": ev.get("ts", ""),
                        "kind": ev.get("kind", ""),
                        "user": ev.get("user", ""),
                        "text": (ev.get("text") or ev.get("thought") or
                                 ev.get("question") or ev.get("say") or
                                 ev.get("tool") or ev.get("reply") or ""),
                        "detail": (ev.get("keywords") or ev.get("result") or
                                   ev.get("reason") or ev.get("args") or
                                   ev.get("error") or ""),
                        "memory": ev.get("memory", ""),
                        "salience": ev.get("salience"),
                        "ok": ev.get("ok"),
                        "latency": ev.get("latency"),
                    })
            except OSError:
                rows = []
            return rows[::-1]
        return self._safe(do)

    def stop_kiri(self):
        def do():
            kiri.state.stopped = not kiri.state.stopped
            return kiri.state.stopped
        return self._safe(do)


# ---- 后台常驻线程 (tick/巩固/联想/主动) ----
def daemon_loop():
    last = time.time()
    last_save = time.time()
    while True:
        now = time.time()
        if now - last >= config.TICK_SECONDS:
            last = now
            try:
                kiri.state.tick()
                kiri._sync_event_mood()   # ★ 事件心情随衰减同步 (M1, 2026-08-27)
                kiri.consolidate_memory()
                # ★ 2026-08-21 雾弥: wander 已取消 — 她做什么由 LLM 自主决定 (完全 MCP 哲学)
                #   不再有系统定时"漫游刷内容"; 她想看世界/找事做时, 由联想念头涌现
                #   (_maybe_act_on_thoughts) 或对话中 agent 自主调工具
                # 联想 (思考为主): 走神 → 念头 (连续意识流, 跨周期接续)
                if kiri.reverie.should_run():
                    kiri.reverie.run_cycle()
                # ★ 话题分析 (原挂在 wander 里, wander 取消后挪到心跳): 证据够多时低频提炼话头
                try:
                    kiri.topic_signals.maybe_analyze()
                except Exception:
                    pass
                kiri.proactive()
            except Exception as exc:
                kiri._log_event("error", where="daemon", error=str(exc))
            # ★ 夜间自主循环: 睡前选阶段(训练OSU/整理记忆), 做完再选下一个
            try:
                import night_loop
                nl = night_loop.NightLoop(kiri)
                if nl.should_run():
                    nl.run_stage()
            except Exception:
                pass
            # ★ 每日日志总结: 睡眠期第一次检查时生成当天回顾 (文件存在防重复)
            try:
                if kiri.state.is_sleeping():
                    today = time.strftime("%Y-%m-%d")
                    summary_path = os.path.join(BASE, "summaries", f"{today}.md")
                    if not os.path.exists(summary_path):
                        import summarize
                        summarize.summarize(today, kiri=kiri)
            except Exception:
                pass
        if now - last_save >= 300:
            last_save = now
            try:
                kiri.state.save()
            except Exception:
                pass
        time.sleep(0.5)


threading.Thread(target=daemon_loop, daemon=True).start()

# ---- 监控面板 (浏览器: http://127.0.0.1:8766) ----
try:
    import monitor_server
    _monitor = monitor_server.MonitorServer(kiri, lambda: _qq_bridge, port=8766)
    _monitor.start()
except Exception as _e:
    print(f"[app] 监控面板启动失败: {_e}")

# ---- 窗口与托盘 ----
_tray_icon = None
_closing = {"quit": False}


def _make_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(120, 170, 255, 255))
    d.ellipse((22, 26, 30, 34), fill=(255, 255, 255, 255))
    d.ellipse((34, 26, 42, 34), fill=(255, 255, 255, 255))
    d.arc((18, 34, 46, 52), start=0, end=180, fill=(255, 255, 255, 255), width=3)
    return img


def _show(name):
    w = _windows.get(name)
    if w:
        try:
            w.show()
            w.restore()
        except Exception:
            pass


def _quit(icon=None, item=None):
    _closing["quit"] = True
    try:
        kiri.state.save()
    except Exception:
        pass
    if _tray_icon:
        try:
            _tray_icon.stop()
        except Exception:
            pass
    for w in _windows.values():
        try:
            w.destroy()
        except Exception:
            pass
    os._exit(0)


def _on_closing(name):
    """关窗口 → 隐藏 (她继续活着)"""
    if _closing["quit"]:
        return True
    w = _windows.get(name)
    if w:
        try:
            w.hide()
        except Exception:
            pass
    return False


def _open_monitor(api):
    mon_url = "file:///" + os.path.join(UI_DIR, "monitor.html").replace("\\", "/")
    w = webview.create_window(
        "Kiri · 后台监控", url=mon_url, js_api=api,
        width=940, height=720, background_color="#0a0f18", on_top=False)
    w.events.closing += lambda: _on_closing("monitor")
    _windows["monitor"] = w
    return w


def _open_trace(api):
    trace_url = "file:///" + os.path.join(UI_DIR, "trace.html").replace("\\", "/")
    w = webview.create_window(
        "Kiri · 回溯", url=trace_url, js_api=api,
        width=960, height=760, background_color="#0a0f18", on_top=False)
    w.events.closing += lambda: _on_closing("trace")
    _windows["trace"] = w
    return w


def main():
    global _tray_icon
    api = Api()
    _open_monitor(api)
    _open_trace(api)

    _tray_icon = pystray.Icon(
        "kiri", _make_icon(), "Kiri — 对外实验版 (后台常驻)",
        menu=pystray.Menu(
            pystray.MenuItem("后台监控", lambda i, it: _show("monitor"), default=True),
            pystray.MenuItem("回溯", lambda i, it: _show("trace")),
            pystray.MenuItem("退出", _quit)))
    threading.Thread(target=_tray_icon.run, daemon=True).start()
    webview.start(debug=False)
    os._exit(0)


if __name__ == "__main__":
    main()
