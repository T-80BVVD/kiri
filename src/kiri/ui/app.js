/* Kiri 桌面应用前端逻辑 (pywebview js_api) */
"use strict";

const $ = (id) => document.getElementById(id);
const msgsEl = $("messages");
let lastHistoryLen = -1;
let api = null;
let voiceOn = true;          // 语音开关
let lastSpokenIdx = -1;      // 已出声的最后一条消息下标
const audioEl = new Audio(); // 语音播放器 (base64 mp3)

function playAudioBase64(b64, key) {
  // ★ Python 后端已双路播放(扬声器+VBCable→VTS原生口型), 前端不再播
  //   也不再注入MouthOpen(避免与VTS lipsync冲突)
}

function fmtTime(ts) {
  if (!ts) return "";
  // 支持 "2026-08-16 09:46:53" / ISO / "HH:MM:SS"
  const m = String(ts).match(/(\d{2}):(\d{2}):\d{2}$/)
         || String(ts).match(/(\d{2}):(\d{2})$/);
  return m ? m[1] + ":" + m[2] : "";
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function renderHistory(history) {
  if (!history || history.length === lastHistoryLen) return;
  lastHistoryLen = history.length;
  // 检测新的主动发言 → 出声
  if (voiceOn && api) {
    for (let i = Math.max(0, lastSpokenIdx + 1); i < history.length; i++) {
      const m = history[i];
      if (m.role === "assistant" && m.content && m.content.startsWith("[主动]")) {
        lastSpokenIdx = i;
        const say = m.content.replace(/^\[主动\]\s*/, "");
        api.tts_text(say).then(r => { if (r && r.audio) playAudioBase64(r.audio); }).catch(() => {});
      }
    }
    if (lastSpokenIdx < 0) lastSpokenIdx = history.length - 1;
  }
  msgsEl.innerHTML = "";
  for (const m of history) {
    const div = document.createElement("div");
    const cls = m.role === "user" ? "user"
      : (m.content && m.content.startsWith("[主动]") ? "proactive" : "assistant");
    div.className = `msg ${cls}`;
    div.textContent = m.content;
    const t = fmtTime(m.ts);
    if (t) {
      const span = document.createElement("span");
      span.className = "time";
      span.textContent = t;
      div.appendChild(span);
    }
    msgsEl.appendChild(div);
  }
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

function renderPanel(data) {
  if (!data) return;
  if (data.error) {
    $("s-stopped").textContent = "桥接错误: " + data.error;
    return;
  }
  const s = data.status || {};
  const fmt = (v) => v === undefined ? "-" : ((v > 0 ? "+" : "") + Number(v).toFixed(2));
  $("s-mood").textContent = fmt(s.mood);
  $("s-pleasure").textContent = fmt(s.pleasure);
  $("s-boredom").textContent = fmt(s.boredom);
  $("s-silence").textContent = s.silence_min === undefined ? "-" : `${s.silence_min} 分钟`;
  $("s-budget").textContent = s.budget_left === undefined ? "-" : `${s.budget_left} 次`;
  $("s-stopped").textContent = s.stopped ? "已暂停" : "活跃中";
  $("stop-btn").textContent = s.stopped ? "恢复主动" : "暂停主动";

  const pros = data.proactives || [];
  $("proactives").innerHTML = pros.length
    ? pros.map(p => `<li><span class="t">${esc(p.ts || "")}</span>${esc(p.say || "")} (${esc(p.reason || "")})</li>`).join("")
    : "<li>还没有主动记录</li>";

  const logs = data.logs || [];
  $("logs").innerHTML = logs.length
    ? logs.map(l => `<li><span class="t">${esc(l.ts || "")}</span>[${esc(l.kind || "")}] ${esc(l.text || "")}</li>`).join("")
    : "<li>暂无日志</li>";

  const mems = data.memories || [];
  $("memories").innerHTML = mems.length
    ? mems.map(m => `<li><span class="t">${esc(m.ts || "")}</span>${esc(m.text || "")}</li>`).join("")
    : "<li>记忆还在沉淀…</li>";
}

async function poll() {
  if (!api) return;
  try {
    const data = await api.get_poll();
    renderPanel(data);
    renderHistory(data.history);
  } catch (e) {
    $("s-stopped").textContent = "桥接错误: " + e;
  }
}

async function send() {
  const text = $("msg-input").value.trim();
  if (!text) return;
  $("msg-input").value = "";
  try {
    const r = await api.send_message(text);
    if (r && r.error) {
      renderHistory([{ role: "assistant", content: "(发送失败: " + r.error + ")" }]);
    } else if (r && r.history) {
      renderHistory(r.history);
      playAudioBase64(r.tts_audio, r.tts_key);   // M5: 回复出声 + 能量驱动嘴型
    }
  } catch (e) {
    renderHistory([{ role: "assistant", content: "(发送失败: " + e + ")" }]);
  }
}

$("send-btn").addEventListener("click", send);
$("msg-input").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
$("stop-btn").addEventListener("click", async () => { if (api) { await api.stop_kiri(); poll(); } });
$("voice-toggle").addEventListener("click", () => {
  voiceOn = !voiceOn;
  $("voice-toggle").textContent = voiceOn ? "🔊" : "🔇";
  $("voice-toggle").classList.toggle("off", !voiceOn);
});

// ★ 轮询等待 pywebview api 桥就绪 (注入时机晚于页面脚本)
(function waitApi() {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.get_poll) {
    api = window.pywebview.api;
    poll();
    setInterval(poll, 2000);
  } else {
    if (!window.__apiWaiting) {
      window.__apiWaiting = true;
      setTimeout(() => {
        $("s-stopped").textContent = "正在连接后端…";
      }, 3000);
    }
    setTimeout(waitApi, 100);
  }
})();
