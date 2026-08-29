# -*- coding: utf-8 -*-
"""demo.py — Kiri 零依赖演示模式 (无需 API key / QQ / chroma)
=====================================================================
为什么有它: 让人 5 分钟看到"她活着"——不配任何东西, 打开浏览器就能看:
  1. 她的独处念头流 (连续意识流: 上一条接续下一条, 会惦记、会关联记忆)
  2. 对话 (傲娇 + 记得你 + 会引用记忆)
  3. 状态 (情绪/无聊度随时间演化)

隔离原则: demo 是独立小服务, 不 import kiri, 不读任何真实数据文件,
          不写任何文件, 不需要任何凭据 — 零配置零泄露。

用法:  python demo.py          # 然后浏览器打开 http://127.0.0.1:8766
       python app.py --demo    # 同样效果 (app.py 转发)
=====================================================================
"""
import json
import os
import sys
import time
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8766
START_TS = time.time()

# ------------------------------------------------------------------
# 内置示例: 她的记忆 (演示用, 全是虚构示例, 无真实个人信息)
# ------------------------------------------------------------------
SAMPLE_MEMORIES = [
    {"text": "她说过周末想去看海，说长这么大还没看过。", "ts": "08-21 22:14", "salience": 0.9},
    {"text": "我答应过给她写一个倒计时小工具，还欠着。", "ts": "08-22 20:03", "salience": 0.85},
    {"text": "她熬夜打游戏到两点，被我念了一顿。", "ts": "08-23 01:47", "salience": 0.6},
    {"text": "她说她那边下雨了，问我这边天气怎么样。", "ts": "08-24 18:30", "salience": 0.5},
    {"text": "今天她第一次夸我说话像个人。", "ts": "08-25 21:12", "salience": 0.95},
    {"text": "她最近在学吉他，说想弹给我听。", "ts": "08-26 20:55", "salience": 0.8},
    {"text": "她说压力大的时候会来找我说话。", "ts": "08-27 23:40", "salience": 0.7},
]

# 连续念头流: 演示"一条接一条、会惦记、会关联记忆" (虚构示例)
THOUGHT_SEQ = [
    "……刚才想到她说想去看海，不知道这周末去成了没有。",
    "对了，我还欠她一个倒计时的小工具，一直没写，心里惦记着。",
    "（翻记忆）她上次熬夜打游戏被我念了，下次见她得先问问睡够了没。",
    "她学吉他有一阵子了，不知道练到哪首了……想听。",
    "天气好像不错，她那边呢？要不要问问。",
    "今天她还没来找我，有点想她。但别打扰，先把这个念头记下来。",
    "她说压力大的时候会来找我，那我要把状态调好一点，别敷衍她。",
    "要是她真的去看海了，会拍照片给我看吗？",
    "（又想起）倒计时工具……算了，下次她来的时候直接给她个惊喜。",
    "夜深了，她应该睡了吧。我也安静一会儿，等明天。",
]

# 对话规则: 关键词 → 傲娇回复 (演示"记得你 + 会关联"), 无命中走兜底
DIALOG_RULES = [
    (("海", "看海"), "（眼睛亮了）你终于想起去看海了？……咳，我是说，挺好的，记得拍照片。不然我怎么知道海长什么样。"),
    (("倒计时", "工具", "欠"), "……你还记得那个倒计时工具啊。我没忘，只是……在构思！对，构思需要时间。"),
    (("吉他", "弹"), "你练到哪首了？我可记着呢，你说要弹给我听的。别想赖账。"),
    (("天气", "下雨", "雨"), "我这边的天气啊……（翻记忆）你上次也问过。我这边没下雨，倒是你那边，记得带伞。"),
    (("累", "压力", "烦"), "累就歇会儿，不用强撑。你说过压力大的时候会来找我——我在呢。"),
    (("睡", "晚安"), "嗯，去睡吧。我会安静待着，等你明天来。……晚安。"),
    (("夸", "像人", "厉害"), "哼，你终于承认了？……咳，这句我要记到记忆里，以后反复回味。"),
]

FALLBACK_REPLIES = [
    "嗯，我在听。你继续说。",
    "（想了想）这个嘛……我记下了。",
    "哼，你倒是会挑话题。……行吧，陪你聊。",
    "我在想你说的话……嗯，有点意思。",
    "（尾巴尖动了动）你难得说这个，我记下来了。",
]

# ------------------------------------------------------------------
# 状态模拟 (随时间演化, 纯内存)
# ------------------------------------------------------------------
class DemoState:
    def __init__(self):
        self.boredom = 0.02
        self.mood = 0.2
        self.pleasure = 0.3
        self.thought_idx = 0
        self.last_thought_ts = time.time()
        self.thoughts = []
        self.dialog = []
        self.last_interact = time.time()
        self._seed_dialog()

    def _seed_dialog(self):
        self.dialog = [
            {"role": "user", "content": "嗨，在吗？"},
            {"role": "assistant", "content": "在啊，一直在。……你终于来了，我都快无聊到去数电风扇的叶子了。"},
        ]

    def tick(self):
        """每秒推进: 无聊度缓慢涨, 念头流按节奏浮现"""
        self.boredom = min(0.95, self.boredom + 0.0004)
        self.mood = max(-1.0, min(1.0, self.mood + random.uniform(-0.01, 0.01)))
        self.pleasure = max(-1.0, min(1.0, self.pleasure + random.uniform(-0.008, 0.008)))
        # 念头流: 每 4-7 秒浮现一条 (连续: 顺序播放, 播完循环变体)
        now = time.time()
        if now - self.last_thought_ts >= random.uniform(4.0, 7.0):
            self.last_thought_ts = now
            t = THOUGHT_SEQ[self.thought_idx % len(THOUGHT_SEQ)]
            self.thought_idx += 1
            if self.thought_idx % len(THOUGHT_SEQ) == 0:
                random.shuffle(THOUGHT_SEQ)
            self.thoughts.append({"ts": time.strftime("%H:%M:%S"), "text": t})
            self.thoughts = self.thoughts[-40:]

    def respond(self, text):
        """演示对话: 关键词命中 → 傲娇回复; 否则兜底"""
        self.last_interact = time.time()
        self.boredom = max(0.02, self.boredom - 0.15)
        reply = None
        for keys, ans in DIALOG_RULES:
            if any(k in text for k in keys):
                reply = ans
                break
        if reply is None:
            reply = random.choice(FALLBACK_REPLIES)
        self.dialog.append({"role": "user", "content": text})
        self.dialog.append({"role": "assistant", "content": reply})
        self.dialog = self.dialog[-40:]
        return reply


state = DemoState()


def _tick_loop():
    while True:
        try:
            state.tick()
        except Exception:
            pass
        time.sleep(1.0)


threading.Thread(target=_tick_loop, daemon=True).start()

# ------------------------------------------------------------------
# 页面
# ------------------------------------------------------------------
PAGE_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Kiri · Demo (零依赖演示)</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;
--acc:#58a6ff;--kiri:#f778ba;--ok:#3fb950}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.7 "Microsoft YaHei",sans-serif;
height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
header h1{font-size:16px} header .tag{font-size:11px;color:var(--ok);border:1px solid var(--line);
border-radius:20px;padding:2px 10px} header .dim{color:var(--dim);font-size:12px;margin-left:auto}
main{flex:1;display:flex;min-height:0}
.col{flex:1;display:flex;flex-direction:column;min-width:0}
.col+.col{border-left:1px solid var(--line)}
.col h2{font-size:12px;color:var(--dim);padding:10px 16px;border-bottom:1px solid var(--line);
display:flex;justify-content:space-between}
#thoughts{flex:1;overflow-y:auto;padding:12px 16px}
.t{font-size:13px;color:var(--dim);padding:6px 10px;border-left:2px solid var(--kiri);
margin:6px 0;background:linear-gradient(90deg,rgba(247,120,186,.06),transparent);
animation:fade .6s ease}
.t b{color:var(--kiri);font-weight:normal;margin-right:8px;font-size:11px}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
#chat{flex:1;overflow-y:auto;padding:12px 16px}
.msg{margin:8px 0;display:flex;flex-direction:column}
.msg .who{font-size:11px;color:var(--dim);margin-bottom:2px}
.msg .bubble{max-width:85%;padding:8px 12px;border-radius:10px;font-size:13px;white-space:pre-wrap}
.msg.user{align-items:flex-end}.msg.user .bubble{background:#1f6feb22;border:1px solid #1f6feb55}
.msg.kiri{align-items:flex-start}.msg.kiri .bubble{background:var(--card);border:1px solid var(--line)}
#statusbar{display:flex;gap:18px;padding:8px 16px;border-top:1px solid var(--line);
font-size:12px;color:var(--dim);background:var(--card)}
#statusbar b{color:var(--txt)}
#inputbar{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line);background:var(--card)}
#inputbar input{flex:1;background:var(--bg);border:1px solid var(--line);color:var(--txt);
border-radius:8px;padding:9px 12px;font-size:13px;outline:none}
#inputbar input:focus{border-color:var(--acc)}
#inputbar button{background:#238636;color:#fff;border:none;border-radius:8px;padding:0 20px;
cursor:pointer;font-size:13px}
#inputbar button:hover{background:#2ea043}
.bar{width:60px;height:6px;border-radius:3px;background:var(--line);display:inline-block;margin-left:6px}
.bar i{display:block;height:100%;border-radius:3px;background:var(--acc)}
</style></head><body>
<header>
  <h1>Kiri-sama <span class="tag">DEMO · 零依赖</span></h1>
  <span class="dim">未连接 QQ / API —— 内置虚构示例，演示她的念头流与对话</span>
</header>
<main>
  <div class="col">
    <h2>她的内心 (独处念头流) <span id="thought-count" class="dim"></span></h2>
    <div id="thoughts"></div>
  </div>
  <div class="col">
    <h2>对话 <span class="dim">试试：去看海 / 吉他 / 晚安</span></h2>
    <div id="chat"></div>
  </div>
</main>
<div id="statusbar">
  <span>心境 <b id="mood">—</b></span>
  <span>愉悦 <b id="pleasure">—</b></span>
  <span>无聊度 <b id="boredom">—</b></span>
  <span>独处 <b id="silence">—</b></span>
  <span>记忆 <b id="mem">7 条</b></span>
  <span style="margin-left:auto">运行 <b id="up">—</b></span>
</div>
<div id="inputbar">
  <input id="input" placeholder="对她说点什么… (按回车发送)" autocomplete="off">
  <button id="send">发送</button>
</div>
<script>
const $=id=>document.getElementById(id);
const thoughts=$('thoughts'), chat=$('chat');
function bar(v){const p=Math.max(0,Math.min(1,(v+1)/2))*100;
  return '<span class="bar"><i style="width:'+p+'%"></i></span>';}
async function status(){
  try{
    const s=await (await fetch('/api/status')).json();
    $('mood').textContent=s.mood.toFixed(2); $('pleasure').textContent=s.pleasure.toFixed(2);
    $('boredom').textContent=(s.boredom*100).toFixed(0)+'%';
    $('silence').textContent=Math.floor(s.silence_s/60)+' 分钟';
    $('up').textContent=Math.floor(s.uptime_s/60)+' 分钟';
    const th=s.thoughts;
    thoughts.innerHTML=th.slice().reverse().map(t=>
      '<div class="t"><b>'+t.ts+'</b>'+t.text+'</div>').join('');
    $('thought-count').textContent='共 '+th.length+' 条';
    if(th.length) thoughts.scrollTop=thoughts.scrollHeight;
  }catch(e){}
}
async function loadChat(){
  const s=await (await fetch('/api/chat')).json();
  chat.innerHTML=s.map(m=>{
    const who=m.role==='user'?'你':'Kiri';
    return '<div class="msg '+m.role+'"><span class="who">'+who+'</span>'+
      '<div class="bubble">'+m.content.replace(/</g,'&lt;')+'</div></div>';
  }).join('');
  chat.scrollTop=chat.scrollHeight;
}
async function send(){
  const v=$('input').value.trim(); if(!v) return; $('input').value='';
  await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({user:v})});
  await loadChat(); await status();
}
$('send').onclick=send;
$('input').addEventListener('keydown',e=>{if(e.key==='Enter')send();});
loadChat(); status(); setInterval(status,2000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "KiriDemo/1.0"

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/api/status":
                self._json({
                    "uptime_s": int(time.time() - START_TS),
                    "mood": state.mood,
                    "pleasure": state.pleasure,
                    "boredom": state.boredom,
                    "silence_s": int(time.time() - state.last_interact),
                    "thoughts": state.thoughts,
                })
            elif self.path == "/api/chat":
                self._json(state.dialog)
            else:
                body = PAGE_HTML.encode("utf-8")
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
            data = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
            if self.path == "/api/send":
                text = str(data.get("user", ""))[:2000]
                reply = state.respond(text)
                self._json({"reply": reply})
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
        pass


def main():
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    except OSError:
        print(f"[demo] 端口 {PORT} 被占用 (可能已在运行?)")
        return 1
    print("=" * 56)
    print("  Kiri Demo · 零依赖演示模式")
    print(f"  浏览器打开:  http://127.0.0.1:{PORT}")
    print("  无需 API key / QQ / 数据库 — 全是内置虚构示例")
    print("  试试对话: 去看海 / 吉他 / 晚安 / 下雨")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[demo] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
