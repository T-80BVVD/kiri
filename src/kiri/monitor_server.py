# -*- coding: utf-8 -*-
"""monitor_server.py — Kiri 后台监控面板 (浏览器访问, 只读, 零依赖)
=====================================================================
用法: app.py 里起一个线程: MonitorServer(kiri, get_bridge, port=8766).start()
页面: http://127.0.0.1:8766   (深色 dashboard, 3秒自动刷新)
API : /api/status → 聚合 JSON (桥/健康/情绪/状态/记忆/工具/事件)
设计: 纯读取 (不碰线上逻辑); 所有字段 try/except 兜底, 监控崩了不影响Kiri
=====================================================================
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))

# ★ 她主动说的消息缓冲 (M3 前置, 2026-08-27): monitor 面板轮询展示
#   app.py 的 _on_proactive 里 push 进来; 前端 /api/proactive?after=N 增量拉取
_PROACTIVE_BUF = []          # [{ts, say, reason}]
_PROACTIVE_SEQ = [0]         # 自增序号
_PROACTIVE_LOCK = threading.Lock()


def push_proactive(say, reason="event"):
    """主动发言推送 (线程安全, 环形缓冲保留最近 100 条)"""
    with _PROACTIVE_LOCK:
        _PROACTIVE_SEQ[0] += 1
        _PROACTIVE_BUF.append({"seq": _PROACTIVE_SEQ[0], "ts": time.time(),
                               "say": str(say)[:300], "reason": str(reason)})
        if len(_PROACTIVE_BUF) > 100:
            del _PROACTIVE_BUF[:-100]


def get_proactive(after=0):
    """增量拉取: 返回 seq > after 的消息 + 当前最新 seq"""
    with _PROACTIVE_LOCK:
        items = [r for r in _PROACTIVE_BUF if r["seq"] > after]
        return items, _PROACTIVE_SEQ[0]


# ---------------- 状态聚合 (纯函数, 可单测) ----------------
def build_status(kiri, bridge):
    """收集全部监控数据 → dict (所有字段兜底)"""
    st = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "uptime_s": 0}
    try:
        st["uptime_s"] = int(time.time() - kiri._start_ts)
    except Exception:
        pass
    # ---- 桥连接 ----
    st["bridge"] = {"connected": False, "groups": 0, "last_msg_ago_s": None,
                    "proactive_queue": 0, "online": None, "online_ago_s": None}
    try:
        if bridge is not None:
            st["bridge"]["connected"] = bridge.ws is not None
            st["bridge"]["groups"] = len(bridge.group_ids)
            st["bridge"]["proactive_queue"] = bridge.proactive_q.qsize()
            if bridge._group_last:
                st["bridge"]["last_msg_ago_s"] = int(time.time() - max(bridge._group_last.values()))
            # ★ 最后收到事件时间 (僵尸连接检测: 这个值一直变大 = ws没在推事件)
            if getattr(bridge, "last_recv_ts", 0):
                st["bridge"]["last_recv_ago_s"] = int(time.time() - bridge.last_recv_ts)
            # ★ 账号在线状态 (踢号检测: WS活着≠账号在线, get_status online=false 才是真相)
            if hasattr(bridge, "online"):
                st["bridge"]["online"] = bridge.online
                st["bridge"]["online_ago_s"] = int(time.time() - bridge.online_since)
    except Exception:
        pass
    # ---- AI 健康 ----
    st["ai"] = {"last_respond_ago_s": None, "last_respond_latency": None,
                "last_tick_ago_s": None, "api_errors_1h": 0}
    try:
        import data_log as data_log_mod
        rows = data_log_mod.read(n=200, kinds=["respond"])
        if rows:
            last = rows[-1]
            from datetime import datetime
            t = datetime.strptime(last.get("ts", ""), "%Y-%m-%d %H:%M:%S")
            st["ai"]["last_respond_ago_s"] = int(time.time() - t.timestamp())
            st["ai"]["last_respond_latency"] = last.get("latency")
    except Exception:
        pass
    try:
        st["ai"]["last_tick_ago_s"] = int(time.time() - kiri.state.last_interact)
    except Exception:
        pass
    try:
        # 最近1小时 API/引擎错误 (events.jsonl)
        err = 0
        ev_path = os.path.join(BASE, "events.jsonl")
        if os.path.exists(ev_path):
            cutoff = time.time() - 3600
            for line in open(ev_path, encoding="utf-8", errors="replace"):
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("kind") == "error":
                    ts = str(e.get("ts", ""))
                    try:
                        if "T" in ts:
                            from datetime import datetime
                            if datetime.fromisoformat(ts).timestamp() > cutoff:
                                err += 1
                        else:
                            from datetime import datetime
                            if datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() > cutoff:
                                err += 1
                    except Exception:
                        pass
        st["ai"]["api_errors_1h"] = err
    except Exception:
        pass
    # ---- 工具调用 (最近20) ----
    st["tools"] = []
    try:
        import mcp_client
        st["tools"] = mcp_client.get_tool_calls(20)
    except Exception:
        pass
    # ---- 情绪 / 状态 ----
    st["emotion"] = {}
    st["activity"] = {}
    st["mood_profile"] = {}
    try:
        e = kiri.state.emotion.state
        deep = e.get("deep_affect", {})
        surf = e.get("surface_emotion", {})
        st["emotion"] = {
            "mood": round(float(deep.get("current_mood", 0)), 3),
            "pleasure": round(float(surf.get("pleasure", 0)), 3),
            "arousal": round(float(surf.get("arousal", 0)), 3),
            "dominance": round(float(surf.get("dominance", 0)), 3),
            "boredom": round(float(kiri.state.boredom), 3),
            "silence_min": round((time.time() - kiri.state.last_interact) / 60, 1),
        }
        try:
            mot = kiri.state.motivation.get_highest_priority_motivation()
            st["emotion"]["motivation"] = mot.get("type", "") if mot else ""
        except Exception:
            pass
        try:
            st["emotion"]["energy"] = round(float(kiri.state.biorhythm.energy), 2)
            st["emotion"]["attention"] = round(float(kiri.state.biorhythm.attention), 2)
        except Exception:
            pass
    except Exception:
        pass
    try:
        snap = kiri.state.activity.snapshot()
        st["activity"] = {"state": snap.get("state"), "skip": snap.get("skip")}
    except Exception:
        pass
    try:
        st["mood_profile"] = kiri.state.mood_profile.stats()
    except Exception:
        pass
    # ---- 记忆库 ----
    st["memory"] = {"users": {}, "knowledge": {}, "anti_repeat": 0, "topic_signals": {}}
    try:
        for u in kiri.memory.users():
            st["memory"]["users"][u] = kiri.memory.count(user=u)
        for u in kiri.memory.users():
            st["memory"]["knowledge"][u] = kiri.knowledge.count(user=u)
    except Exception:
        pass
    try:
        st["memory"]["anti_repeat"] = len(kiri.anti_repeat._corpus)
    except Exception:
        pass
    try:
        st["memory"]["topic_signals"] = kiri.topic_signals.stats()
    except Exception:
        pass
    # ---- 工作记忆 (每用户最近对话 + 最近念头) ----
    st["working"] = {"dialogs": {}, "thoughts": []}
    try:
        for u in kiri.memory.users()[:8]:
            dlg = kiri.get_dialog(u)
            st["working"]["dialogs"][u] = [m.get("text", "")[:60] for m in dlg[-6:]]
        st["working"]["thoughts"] = [t[:80] for t in kiri.state.thoughts[-5:]]
    except Exception:
        pass
    # ---- 事件流尾部 ----
    st["events"] = []
    try:
        ev_path = os.path.join(BASE, "events.jsonl")
        if os.path.exists(ev_path):
            lines = open(ev_path, encoding="utf-8", errors="replace").read().splitlines()
            for ln in lines[-25:]:
                try:
                    e = json.loads(ln)
                    st["events"].append({
                        "ts": e.get("ts", "")[11:19] if len(e.get("ts", "")) >= 19 else e.get("ts", ""),
                        "kind": e.get("kind", ""),
                        "text": (e.get("reply") or e.get("thought") or e.get("question")
                                 or e.get("say") or e.get("user") or "")[:70],
                    })
                except Exception:
                    pass
    except Exception:
        pass
    # ---- 决策轨迹 (最近回合, think→工具→结果→speak) ----
    st["trace"] = []
    try:
        ev_path = os.path.join(BASE, "events.jsonl")
        if os.path.exists(ev_path):
            trace = []
            for ln in open(ev_path, encoding="utf-8", errors="replace").read().splitlines():
                try:
                    e = json.loads(ln)
                except Exception:
                    continue
                if e.get("kind") == "agent_trace":
                    trace.append({"ts": e.get("ts", ""), "user": e.get("user", ""),
                                  "steps": e.get("steps", []), "reply": e.get("reply", "")})
            st["trace"] = trace[-25:][::-1]   # 最近25条, 新的在前
    except Exception:
        pass
    return st


# ---------------- HTTP 服务 ----------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Kiri 监控</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 "Microsoft YaHei",sans-serif;padding:18px;max-width:1200px;margin:0 auto}
h1{font-size:18px} .sub{color:var(--dim);font-size:12px;margin:4px 0 14px}
.toolbar{display:flex;flex-wrap:wrap;gap:6px 16px;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:13px}
.toolbar label{cursor:pointer;user-select:none;display:flex;align-items:center;gap:5px}
.toolbar input{accent-color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;display:none}
.card.on{display:block}
.card h2{font-size:13px;color:var(--acc);margin-bottom:10px;border-bottom:1px solid var(--line);padding-bottom:8px}
.kv{display:grid;grid-template-columns:120px 1fr;gap:5px 10px;font-size:13px}
.kv b{color:var(--dim);font-weight:normal;text-align:right;white-space:nowrap}
.kv span{word-break:break-all}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.ok{background:var(--ok)} .bad{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
td.t{color:var(--dim)} .tool-ok{color:var(--ok)} .tool-bad{color:var(--bad)}
.ev{font-size:12px;color:var(--dim);padding:3px 0;border-bottom:1px solid var(--line)}
.ev b{color:var(--txt)} .ev .k{color:var(--acc);margin-right:6px}
#events,#tools{max-height:300px;overflow-y:auto}
</style></head><body>
<h1>Kiri-sama 后台监控</h1>
<div class="sub" id="meta">加载中...</div>
<div class="toolbar" id="toolbar">
  <label><input type="checkbox" data-sec="bridge" checked>桥连接</label>
  <label><input type="checkbox" data-sec="ai" checked>AI健康</label>
  <label><input type="checkbox" data-sec="emotion">情绪</label>
  <label><input type="checkbox" data-sec="activity">活动</label>
  <label><input type="checkbox" data-sec="memory">记忆库</label>
  <label><input type="checkbox" data-sec="working">工作记忆</label>
  <label><input type="checkbox" data-sec="tools">工具调用</label>
  <label><input type="checkbox" data-sec="events" checked>事件流</label>
  <span style="margin-left:auto;color:var(--dim)">勾选要看的区块 (自动保存)</span>
</div>
<div class="grid">
  <div class="card" data-sec="bridge"><h2>桥连接</h2><div class="kv" id="bridge"></div></div>
  <div class="card" data-sec="ai"><h2>AI 健康</h2><div class="kv" id="ai"></div></div>
  <div class="card" data-sec="emotion"><h2>情绪</h2><div class="kv" id="emotion"></div></div>
  <div class="card" data-sec="activity"><h2>活动状态</h2><div class="kv" id="activity"></div></div>
  <div class="card" data-sec="memory"><h2>记忆库</h2><div class="kv" id="memory"></div></div>
  <div class="card" data-sec="working"><h2>工作记忆</h2><div class="kv" id="working"></div></div>
  <div class="card" data-sec="tools"><h2>工具调用 (最近)</h2>
    <table><thead><tr><th>时间</th><th>工具</th><th>参数</th><th>结果</th><th>延迟</th></tr></thead>
    <tbody id="tools"></tbody></table></div>
  <div class="card" data-sec="events"><h2>事件流</h2><div id="events"></div></div>
</div>
<script>
// 区块选择: localStorage 持久化
const KEY='kiri_mon_secs';
function loadSecs(){
  try{const s=JSON.parse(localStorage.getItem(KEY)||'{}');
    document.querySelectorAll('#toolbar input').forEach(cb=>{
      const sec=cb.dataset.sec;
      if(sec in s) cb.checked=s[sec];
      document.querySelectorAll('.card[data-sec="'+sec+'"]').forEach(c=>{
        c.classList.toggle('on', cb.checked);});
    });
  }catch(e){}
}
document.querySelectorAll('#toolbar input').forEach(cb=>{
  cb.addEventListener('change',()=>{
    const sec=cb.dataset.sec, s={};
    document.querySelectorAll('#toolbar input').forEach(c=>s[c.dataset.sec]=c.checked);
    localStorage.setItem(KEY,JSON.stringify(s));
    document.querySelectorAll('.card[data-sec="'+sec+'"]').forEach(c=>c.classList.toggle('on',cb.checked));
  });
});
loadSecs();
async function refresh(){
  try{
    const s=await (await fetch('/api/status')).json();
    document.getElementById('meta').textContent =
      `更新: ${s.ts} | 运行: ${Math.floor(s.uptime_s/3600)}h${Math.floor(s.uptime_s%3600/60)}m | http://127.0.0.1:8766`;
    const b=s.bridge;
    document.getElementById('bridge').innerHTML =
      `<b>连接</b><span><span class="dot ${b.connected?'ok':'bad'}"></span>${b.connected?'已连接':'断开'}</span>
       <b>账号在线</b><span><span class="dot ${b.online?'ok':'bad'}"></span>${b.online?'在线':'离线!'}${b.online_ago_s!=null?'('+b.online_ago_s+'s)':''}</span>
       <b>已知群</b><span>${b.groups} 个</span>
       <b>最后消息</b><span>${b.last_msg_ago_s==null?'—':b.last_msg_ago_s+'秒前'}</span>
       <b>最后收到事件</b><span class="${b.last_recv_ago_s>300?'tool-bad':''}">${b.last_recv_ago_s==null?'—':b.last_recv_ago_s+'秒前(>30min会强制重连)'}</span>
       <b>主动队列</b><span>${b.proactive_queue}</span>`;
    const a=s.ai;
    document.getElementById('ai').innerHTML =
      `<b>最后回应</b><span>${a.last_respond_ago_s==null?'—':a.last_respond_ago_s+'秒前'}</span>
       <b>回应延迟</b><span>${a.last_respond_latency==null?'—':a.last_respond_latency+'s'}</span>
       <b>最后互动</b><span>${a.last_tick_ago_s==null?'—':a.last_tick_ago_s+'秒前'}</span>
       <b>API错误/时</b><span class="${a.api_errors_1h>0?'tool-bad':''}">${a.api_errors_1h}</span>`;
    const e=s.emotion;
    document.getElementById('emotion').innerHTML =
      `<b>心境</b><span>${e.mood??'—'}</span><b>愉悦</b><span>${e.pleasure??'—'}</span>
       <b>唤醒</b><span>${e.arousal??'—'}</span><b>支配</b><span>${e.dominance??'—'}</span>
       <b>无聊度</b><span>${e.boredom??'—'}</span><b>沉默</b><span>${e.silence_min??'—'}分钟</span>
       <b>动机</b><span>${e.motivation||'—'}</span><b>精力/注意</b><span>${e.energy??'—'}/${e.attention??'—'}</span>`;
    const act=s.activity;
    document.getElementById('activity').innerHTML =
      `<b>状态</b><span>${act.state||'—'}</span>
       <b>雾弥情绪画像</b><span>${Object.entries(s.mood_profile||{}).map(([u,v])=>u+'('+v.val+')').join(' ')||'—'}</span>`;
    const m=s.memory;
    document.getElementById('memory').innerHTML =
      `<b>记忆条数</b><span>${Object.entries(m.users).map(([u,c])=>u+': '+c).join('，')||'—'}</span>
       <b>知识页</b><span>${Object.entries(m.knowledge).map(([u,c])=>u+': '+c).join('，')||'—'}</span>
       <b>防复读语料</b><span>${m.anti_repeat} 条</span>
       <b>话题信号</b><span>${m.topic_signals.signals??0} 条 / 话头${m.topic_signals.materials??0}</span>`;
    const w=s.working;
    document.getElementById('working').innerHTML =
      Object.entries(w.dialogs).slice(0,3).map(([u,lines])=>
        `<b>${u}</b><span>${lines.slice(-3).join(' … ')||'—'}</span>`).join('') +
      `<b>最近念头</b><span>${w.thoughts.length?w.thoughts.join(' / '):'—'}</span>`;
    document.getElementById('tools').innerHTML = (s.tools||[]).map(t=>
      `<tr><td class="t">${t.ts}</td><td>${t.tool}</td><td>${t.args}</td>
       <td class="${t.ok?'tool-ok':'tool-bad'}">${String(t.result||'').slice(0,40)}</td>
       <td class="t">${t.latency}s</td></tr>`).join('') || '<tr><td colspan="5">(无)</td></tr>';
    document.getElementById('events').innerHTML = (s.events||[]).slice().reverse().map(ev=>
      `<div class="ev"><span class="k">${ev.kind}</span><b>${ev.ts}</b> ${ev.text}</div>`).join('');
  }catch(err){ document.getElementById('meta').textContent='加载失败: '+err; }
}
refresh(); setInterval(refresh,3000);
</script></body></html>"""


def _load_dashboard():
    """优先读外部 ui/monitor_app.html (新冷调浅色 SPA); 读不到回退内置 DASHBOARD_HTML"""
    try:
        p = os.path.join(BASE, "ui", "monitor_app.html")
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    except Exception:
        pass
    return DASHBOARD_HTML


class _Handler(BaseHTTPRequestHandler):
    server_version = "KiriMonitor/1.0"

    def do_GET(self):
        try:
            if self.path == "/api/status":
                body = json.dumps(build_status(self.server.kiri, self.server.get_bridge()),
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/proactive"):
                # ★ 她主动说的消息 (M3 前置): /api/proactive?after=N → {"items": [...], "latest": N}
                try:
                    after = int(self.path.split("after=")[1].split("&")[0]) if "after=" in self.path else 0
                except Exception:
                    after = 0
                items, latest = get_proactive(after)
                body = json.dumps({"items": items, "latest": latest},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = _load_dashboard().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception:
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def do_POST(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln).decode("utf-8") if ln else "{}"
            data = json.loads(body or "{}")
            if self.path == "/api/chat":
                user_text = str(data.get("user", ""))[:2000]
                try:
                    # ★ 调试输入通道 (非QQ): 调 Kiri 正常 respond (current_user 身份/正常记忆)
                    #   这只是"我在控制台输入"调试 Kiri 用; 不代表 QQ 通道好/坏 (QQ 看 bridge 指标)
                    reply = self.server.kiri.respond(user_text, scene="private")
                    resp = {"reply": reply}
                except Exception as e:
                    resp = {"reply": f"(输入通道出错: {e})", "error": True}
                out = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if self.path == "/api/chat_stream":
                # ★ 网页聊天流式 (2026-08-27): SSE 推文本片段; 前端按 <sep> 分条显示 (QQ式)
                user_text = str(data.get("user", ""))[:2000]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                emitted = [False]
                def _emit(chunk):
                    if not chunk:
                        return
                    emitted[0] = True
                    try:
                        self.wfile.write(("data: " + json.dumps({"chunk": chunk},
                                              ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                try:
                    reply = self.server.kiri.respond(user_text, scene="private", on_reply_token=_emit)
                    # 兜底: 非流式路径(旧分支)未回调 → 整段补发
                    if reply and not emitted[0]:
                        _emit(reply)
                except Exception as e:
                    _emit("(输入通道出错: %s)" % str(e)[:100])
                finally:
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                return
            self.send_response(404)
            self.end_headers()
        except Exception:
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, fmt, *args):
        pass   # 静默


class MonitorServer:
    """只读监控 HTTP 服务 (线程)"""

    def __init__(self, kiri, get_bridge, port=8766):
        self.kiri = kiri
        self.get_bridge = get_bridge
        self.port = port
        self._httpd = None
        self._thread = None

    def start(self):
        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
            self._httpd.kiri = self.kiri
            self._httpd.get_bridge = self.get_bridge
            self._thread = threading.Thread(target=self._httpd.serve_forever,
                                            daemon=True, name="monitor-http")
            self._thread.start()
            print(f"[monitor] 监控面板: http://127.0.0.1:{self.port}")
            return True
        except Exception as e:
            print(f"[monitor] 启动失败: {e}")
            return False

    def stop(self):
        try:
            if self._httpd:
                self._httpd.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    # 自测: 无Kiri时也能起服务 (字段兜底为—)
    srv = MonitorServer(kiri=None, get_bridge=lambda: None, port=8766)
    if srv.start():
        print("启动OK, 浏览器打开 http://127.0.0.1:8766")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            srv.stop()
