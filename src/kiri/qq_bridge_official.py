# -*- coding: utf-8 -*-
"""Kiri QQ 桥 — 官方 API 版 (v2, 2026-08-27)
=====================================================================
背景: NapCat 注入方案被 QQ 更新搞崩 (文件已损坏) + 踢号无根治。
      官方 API 不会踢号、国内直连不需要代理 — 根治两条痛点。

协议 (官方 v2 API):
  - WebSocket 网关: GET https://api.sgroup.qq.com/gateway → wss://api.sgroup.qq.com/websocket/
  - 鉴权: token = "QQBot {access_token}" (头 Authorization)
    access_token 从 POST https://bots.qq.com/app/getAppAccessToken 换 (appId+clientSecret, 2h有效)
  - 心跳: op=1 携最新 s; 断线短时重连用 op=6 Resume (session_id+seq) 补发遗漏事件
  - 事件: GROUP_AT_MESSAGE_CREATE (Intent 1<<25) — 群里@机器人, content 已去@前缀
  - 发送: POST /v2/groups/{group_openid}/messages {"content","msg_type":0,"msg_seq"}
  - 去重: 相同 msg_id 可能重复推送, 结合 msg_seq 去重
  - 被动模式: 平台只推@机器人的消息 → "只能在被@时说话"天然成立, 主动行为内部化

配置 (qq_config.json):
  { "app_id": "YOUR_APP_ID", "client_secret": "你的Secret",
    "owner_openid": "" }   # 雾弥的 openid (可选, 恋人线识别; 未填则从对话中学习)

依赖: 标准库 urllib + websockets (已有)
用法: start(cfg, respond_fn, kiri) — 与 qq_bridge.py 同接口, app.py 二选一
=====================================================================
"""
import asyncio
import json
import os
import queue
import threading
import time
import urllib.request

import websockets

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "qq_config.json")
GROUPS_FILE = os.path.join(BASE, "qq_official_groups.json")   # 已知群持久化

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL = API_BASE + "/gateway"
WS_PATH = "/websocket/"
INTENT_GROUP_AND_C2C = 1 << 25          # 群@ + 单聊事件
MSG_TYPE_TEXT = 0

# 主动内部化 (被动模式): 官方 API 无"主动发群消息"能力 (需白名单+模板审核),
# 她的"想说"只进念头库不外发 — 由 kiri.proactive 决定 (不投递)
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]


def load_config():
    """读配置; 缺 app_id/secret → 桥不启动"""
    cfg = {"app_id": "", "client_secret": "", "owner_openid": ""}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


# =====================================================================
# token 管理 (2h 有效期, 提前 60s 刷新)
# =====================================================================
class TokenManager:
    def __init__(self, app_id, client_secret):
        self.app_id = str(app_id)
        self.client_secret = str(client_secret)
        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self):
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            body = json.dumps({"appId": self.app_id,
                               "clientSecret": self.client_secret}).encode("utf-8")
            req = urllib.request.Request(TOKEN_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            token = d.get("access_token")
            if not token:
                raise RuntimeError(f"QQ token 获取失败: {d}")
            self._token = token
            self._expires_at = time.time() + int(d.get("expires_in", 7200))
            return token


# =====================================================================
# 官方桥
# =====================================================================
class QQBridgeOfficial:
    """官方 API 桥 (v2): WS 网关接收@消息 → Kiri respond → REST 发群消息"""

    # 僵尸连接防护: 静默30分钟无事件 或 连接超3小时 → 强制重连
    WS_SILENCE_RECONNECT = 1800
    WS_MAX_AGE = 3 * 3600

    def __init__(self, cfg, respond_fn, kiri=None):
        self.cfg = cfg
        self.respond = respond_fn          # respond(text, user, scene) → str
        self.kiri = kiri
        self.tokens = TokenManager(cfg.get("app_id", ""), cfg.get("client_secret", ""))
        self.ws = None
        self.q = asyncio.Queue()           # 消息事件 (单worker串行回复)
        self.proactive_q = queue.Queue()   # 主动内部化队列 (仅记录, 不投递)
        self._worker_task = None
        self._heartbeat_task = None
        self._seq = 0                      # 最新事件序列号 (心跳/Resume用)
        self._session_id = ""
        self._last_recv_ts = 0.0
        self._conn_start = 0.0
        self._seen_msg = set()             # msg_id 去重 (环形上限)
        self._msg_seq_map = {}             # msg_id → msg_seq (重复推送去重)
        self.group_ids = set()             # 见过的群 openid
        self._group_last = {}
        self._load_groups()
        self._last_private_chat = None     # 最近活跃单聊 openid (重连通知用)
        self.group_history = {}            # group_openid → 最近消息 [(who, text)]
        self.online = True
        self.online_since = time.time()
        self._pending_sends = []           # 掉线期间消息缓存 (限50)
        self._down_since = 0.0
        self._send_lock = asyncio.Lock()
        self._msg_seq_counter = [0]        # 发送消息 msg_seq 自增

    # ---------- 已知群持久化 ----------
    def _load_groups(self):
        try:
            if os.path.exists(GROUPS_FILE):
                with open(GROUPS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                for gid, ts in (data or {}).items():
                    self.group_ids.add(str(gid))
                    self._group_last[str(gid)] = float(ts)
        except Exception:
            pass

    def _save_groups(self):
        try:
            with open(GROUPS_FILE, "w", encoding="utf-8") as f:
                json.dump({g: self._group_last.get(g, 0) for g in self.group_ids},
                          f, ensure_ascii=False)
        except Exception:
            pass

    # ---------- 网关 ----------
    async def _get_gateway_url(self):
        token = self.tokens.get()
        req = urllib.request.Request(GATEWAY_URL, headers={"Authorization": f"QQBot {token}"})
        # 同步调用放线程池, 避免阻塞事件循环
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._http_get_json, req)
        url = data.get("url", "")
        if not url:
            raise RuntimeError(f"网关地址获取失败: {data}")
        return url

    def _http_get_json(self, req):
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_post_json(self, url, body_dict, headers=None):
        body = json.dumps(body_dict).encode("utf-8")
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=body, headers=h)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---------- 事件去重 (msg_id 可能重复推送) ----------
    def _is_dup(self, msg_id, msg_seq):
        if not msg_id:
            return False
        prev = self._msg_seq_map.get(msg_id)
        if prev is None:
            if len(self._msg_seq_map) > 5000:
                self._msg_seq_map.clear()
            self._msg_seq_map[msg_id] = msg_seq
            return False
        # 同 msg_id: seq 相同 = 重复推送
        return prev == msg_seq

    # ---------- 主循环 ----------
    async def run(self):
        """连接网关 → 鉴权 → 心跳 → 处理事件 (断线按退避重连, 短断 Resume 补发)"""
        attempt = 0
        last_session = None
        while True:
            try:
                url = await self._get_gateway_url()
                if not url.startswith("ws"):
                    url = "wss://api.sgroup.qq.com" + WS_PATH
                self._log_bridge(f"连接网关 {url}")
                self._conn_start = time.time()
                async with websockets.connect(url, max_size=None) as ws:
                    self.ws = ws
                    attempt = 0
                    # 短断重连: 有 session → Resume 补发; 否则 Identify
                    if last_session:
                        await self._send_json({"op": 6, "d": {
                            "token": f"QQBot {self.tokens.get()}",
                            "session_id": self._session_id, "seq": self._seq}})
                    else:
                        await self._send_json({"op": 2, "d": {
                            "token": f"QQBot {self.tokens.get()}",
                            "intents": INTENT_GROUP_AND_C2C,
                            "shard": [0, 1],
                            "properties": {"$os": "windows", "$browser": "kiri",
                                           "$device": "kiri"}}})
                    await self._event_loop(ws)
            except Exception as e:
                self._log_bridge(f"连接异常: {e}")
                last_session = True   # 短断尝试 Resume
            finally:
                self.ws = None
                self.online = False
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            self._log_bridge(f"{delay}s 后重连")
            await asyncio.sleep(delay)
            attempt += 1

    async def _send_json(self, payload):
        if self.ws:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def _event_loop(self, ws):
        """事件循环: Hello→心跳; Dispatch→事件处理; Reconnect→重连"""
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
            except asyncio.TimeoutError:
                # 90s 无下行 → 发心跳保活; 仍无 → 断开重连
                await self._send_json({"op": 1, "d": self._seq})
                continue
            except Exception:
                return
            self._last_recv_ts = time.time()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            op = msg.get("op")
            if op == 10:    # Hello
                interval = int(msg.get("d", {}).get("heartbeat_interval", 45000))
                self._log_bridge(f"网关就绪, 心跳 {interval}ms")
                self.online = True
                self.online_since = time.time()
                if self._heartbeat_task is None or self._heartbeat_task.done():
                    self._heartbeat_task = asyncio.create_task(self._heartbeat(interval / 1000))
            elif op == 11:  # Heartbeat ACK
                pass
            elif op == 0:   # Dispatch
                if msg.get("s"):
                    self._seq = msg["s"]
                t = msg.get("t")
                if t == "READY":
                    d = msg.get("d", {})
                    self._session_id = d.get("session_id", "")
                    self._log_bridge(f"鉴权成功 session={self._session_id[:8]}...")
                elif t == "RESUMED":
                    self._log_bridge("连接已恢复 (补发完成)")
                else:
                    self._handle_dispatch(t, msg.get("d", {}))
            elif op == 7:   # Reconnect
                self._log_bridge("服务端要求重连")
                return

    async def _heartbeat(self, interval):
        """周期心跳 (携带最新 seq; 失败则退出循环触发重连)"""
        try:
            while True:
                await asyncio.sleep(interval)
                await self._send_json({"op": 1, "d": self._seq})
        except Exception:
            pass

    # ---------- 事件分发 ----------
    def _handle_dispatch(self, t, d):
        if t == "GROUP_AT_MESSAGE_CREATE":
            asyncio.ensure_future(self._on_group_at(d))
        elif t == "C2C_MESSAGE_CREATE":
            asyncio.ensure_future(self._on_c2c(d))
        elif t == "GROUP_ADD_ROBOT":
            gid = d.get("group_openid", "")
            if gid:
                self.group_ids.add(gid)
                self._group_last[gid] = time.time()
                self._save_groups()
                self._log_bridge(f"被拉入群 {gid}")
        # FRIEND_ADD / GROUP_DEL_ROBOT 等忽略 (被动模式不处理)

    def _display_name(self, d):
        """发送者显示名: owner_openid → 雾弥; 否则用昵称/未知"""
        author = d.get("author", {}) or {}
        oid = author.get("member_openid", "") or author.get("user_openid", "")
        nick = author.get("member_openid", "")
        if self.cfg.get("owner_openid") and oid == self.cfg["owner_openid"]:
            return "雾弥"
        return oid or "未知"

    async def _on_group_at(self, d):
        """群里@机器人 → respond (被动模式核心)"""
        msg_id = str(d.get("id", ""))
        msg_seq = d.get("msg_seq")
        if self._is_dup(msg_id, msg_seq):
            return
        gid = d.get("group_openid", "")
        content = str(d.get("content", "")).strip()
        if gid:
            self.group_ids.add(gid)
            self._group_last[gid] = time.time()
            self._save_groups()
        if not content:
            return
        author = d.get("author", {}) or {}
        user_openid = author.get("member_openid", "") or author.get("user_openid", "")
        who = self._display_name(d)
        self._log_bridge(f"[群@{gid[:8]}] {who}: {content[:60]}")
        # 群上下文 (最近几条, 喂给 respond 防单条错配)
        ctx = self._group_context(gid)
        try:
            reply = await self._safe_respond(content, user_openid, scene="group",
                                             group_context=ctx)
        except Exception:
            return
        if reply:
            await self._send_group_text(gid, reply, reply_to=msg_id)

    async def _on_c2c(self, d):
        """单聊机器人 → respond (私聊线)"""
        msg_id = str(d.get("id", ""))
        msg_seq = d.get("msg_seq")
        if self._is_dup(msg_id, msg_seq):
            return
        content = str(d.get("content", "")).strip()
        author = d.get("author", {}) or {}
        user_openid = author.get("user_openid", "")
        who = self._display_name(d)
        self._last_private_chat = user_openid
        self._log_bridge(f"[单聊] {who}: {content[:60]}")
        if not content:
            return
        try:
            reply = await self._safe_respond(content, user_openid, scene="private")
        except Exception:
            return
        if reply:
            await self._send_c2c_text(user_openid, reply, reply_to=msg_id)

    def _group_context(self, gid, n=4):
        """最近群消息上下文 (喂给 respond)"""
        lines = self.group_history.get(gid, [])
        return [t for _, t in lines[-n:]] if lines else None

    # ---------- 发送 ----------
    def _next_msg_seq(self):
        self._msg_seq_counter[0] += 1
        return self._msg_seq_counter[0] % (1 << 31)

    async def _send_group_text(self, group_openid, text, reply_to=None):
        token = self.tokens.get()
        body = {"content": text[:4000], "msg_type": MSG_TYPE_TEXT,
                "msg_seq": self._next_msg_seq()}
        if reply_to:
            body["msg_id"] = reply_to      # 被动回复: 关联被@的那条
        url = f"{API_BASE}/v2/groups/{group_openid}/messages"
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._http_post_json, url, body,
                                       {"Authorization": f"QQBot {token}"})
            self._log_bridge(f"群消息已发 ({group_openid[:8]}): {text[:40]}")
        except Exception as e:
            self._log_bridge(f"群消息发送失败: {e}")

    async def _send_c2c_text(self, user_openid, text, reply_to=None):
        token = self.tokens.get()
        body = {"content": text[:4000], "msg_type": MSG_TYPE_TEXT,
                "msg_seq": self._next_msg_seq()}
        if reply_to:
            body["msg_id"] = reply_to
        url = f"{API_BASE}/v2/users/{user_openid}/messages"
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._http_post_json, url, body,
                                       {"Authorization": f"QQBot {token}"})
            self._log_bridge(f"单聊消息已发 ({user_openid[:8]}): {text[:40]}")
        except Exception as e:
            self._log_bridge(f"单聊消息发送失败: {e}")

    # ---------- 主动内部化 (被动模式) ----------
    def enqueue_proactive(self, say):
        """app 的 _on_proactive 调用: 官方模式不投递 — 只记录 + 写心路
        (她的"想说"进念头库/日志, 等雾弥@她时再自然说出)"""
        try:
            self.proactive_q.put((time.time(), str(say)[:200]))
        except Exception:
            pass
        self._log_bridge(f"[主动内部化] (不投递) {say[:40]}")

    # ---------- 工具 ----------
    async def _safe_respond(self, text, target_user, scene="private", group_context=None):
        """调 Kiri respond (同步 → 线程池), 防阻塞事件循环"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.respond, text, target_user, scene, group_context)

    def _log_bridge(self, msg):
        print(f"[QQ官方桥] {msg}", flush=True)


# =====================================================================
# 启动入口 (与 qq_bridge.start 同签名)
# =====================================================================
def start(cfg, respond_fn, kiri=None):
    bridge = QQBridgeOfficial(cfg, respond_fn, kiri)

    def _run():
        try:
            asyncio.run(bridge.run())
        except Exception as e:
            print(f"[QQ官方桥] 启动失败: {e}", flush=True)
    t = threading.Thread(target=_run, daemon=True, name="qq-official")
    t.start()
    return bridge
