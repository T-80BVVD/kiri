# -*- coding: utf-8 -*-
"""Kiri QQ 桥 — OneBot 11 协议 (NapCatQQ 正向 WebSocket)
=====================================================================
角色: Kiri 是 QQ 上的"她" — 通过 NapCat 收发消息
- 私聊: 收到就回; 雾弥(owner_qq)用恋人线+自己的库, 其他人自动建库走朋友线
- 群聊: 只在被 @ 时回 (避免刷屏)
- 主动发言: app 的 _on_proactive 推入队列, 桥转发给雾弥的 QQ
配置: qq_config.json
  { "ws_url": "ws://127.0.0.1:3001",   # NapCat 正向WS地址(默认3001)
    "owner_qq": "1XXXXXXXX",            # 雾弥的QQ号 → 恋人线
    "bot_qq":   "1XXXXXXXX" }           # 她的QQ号 (群聊@识别)
依赖: pip install websockets
测试: python test_qq_bridge.py (本地mock服务器, 不连真QQ)
=====================================================================
"""
import asyncio
import json
import os
import queue
import re
import sys
import threading
import time

import websockets

BASE = os.path.dirname(os.path.abspath(__file__))

# ★ 踢号自动重登 (2026-08-27): 桥检测踢号 → 写 kick_state.json → qq_guardian 定时重登
KICK_STATE_FILE = os.path.join(BASE, "kick_state.json")
KICK_RELOGIN_COOLDOWN_MIN = 30      # 踢号后冷却 (分钟), 等风控窗口过去再重登
KICK_CATCHUP_TEXT = "我刚刚掉线了一下，现在回来啦。"   # 重连后向最近私聊补发; 置空=关闭
CONFIG_FILE = os.path.join(BASE, "qq_config.json")
GROUPS_FILE = os.path.join(BASE, "qq_groups.json")   # ★ 已知群列表持久化 (重启不丢)


def load_config():
    """读配置; 缺失/空 owner_qq → 桥不启动 (等填好再开)"""
    cfg = {"ws_url": "ws://127.0.0.1:3001", "owner_qq": "", "bot_qq": ""}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def extract_text(msg, bot_qq=""):
    """OneBot 11 message → 纯文本 (数组段 或 CQ码字符串)
    ★ @ 保留为文本 (治"分不清谁在对谁说话"):
      - @她自己 → "@Kiri"
      - @其他人 → "@QQ号"
      - @所有人 → "@所有人"
    bot_qq: 她的QQ号, 用于把自己的@显示成名字"""
    if isinstance(msg, str):
        def _cq(m):
            tag = m.group(0)
            if tag.startswith("[CQ:at"):
                q = re.search(r"qq=(\d+)", tag)
                if not q:
                    return ""
                qq = q.group(1)
                if bot_qq and qq == str(bot_qq):
                    return "@Kiri "
                return f"@{qq} "
            return ""
        return re.sub(r"\[CQ:[^\]]*\]", _cq, msg).strip()
    parts = []
    for seg in msg or []:
        if isinstance(seg, dict):
            if seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
            elif seg.get("type") == "at":
                q = str(seg.get("data", {}).get("qq", ""))
                if q == "all":
                    parts.append("@所有人 ")
                elif bot_qq and q == str(bot_qq):
                    parts.append("@Kiri ")
                elif q:
                    parts.append(f"@{q} ")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def extract_at_qq(msg):
    """取被@的QQ (列表形式)"""
    if not isinstance(msg, list):
        return []
    out = []
    for seg in msg:
        if isinstance(seg, dict) and seg.get("type") == "at":
            q = str(seg.get("data", {}).get("qq", ""))
            if q:
                out.append(q)
    return out


def _split_sentences(text, max_len=40):
    """回复拆小段 (像人发消息一条条发): 按句末标点(。！？～…!?)拆
    括号动作前缀跟第一段; 过短段(<8字)并入前一段, 避免碎成'嗯。''好。'"""
    import re as _re
    t = str(text or "").strip()
    if not t:
        return []
    parts = [p.strip() for p in _re.split(r"(?<=[。！？～…!?])", t) if p.strip()]
    merged = []
    for p in parts:
        if merged and len(p) < 8 and len(merged[-1]) + len(p) <= max_len:
            merged[-1] += p
        else:
            merged.append(p)
    return merged


# =====================================================================
# ★ 2026-08-21 直接移植 NachoBot response_pool.py (echo 响应池):
#   API 调用带 echo 回执 → 拿群名/成员信息/被引用消息原文
#   原版: src/response_pool.py (轮询字典 + 超时清理)
# =====================================================================
_RESPONSE_DICT = {}          # echo_id -> 响应 dict
_RESPONSE_TIME_DICT = {}     # echo_id -> 存入时间
HEARTBEAT_INTERVAL = 30.0    # NapCat 心跳间隔 (超时清理阈值)


async def get_response(request_id, timeout=10):
    """等待 echo 响应 (轮询 + 超时)"""
    response = await asyncio.wait_for(_get_response(request_id), timeout)
    _RESPONSE_TIME_DICT.pop(request_id, None)
    return response


async def _get_response(request_id):
    while request_id not in _RESPONSE_DICT:
        await asyncio.sleep(0.2)
    return _RESPONSE_DICT.pop(request_id)


async def put_response(response):
    """主循环收到 echo 响应 → 存入响应池"""
    echo_id = response.get("echo")
    if not echo_id:
        return
    _RESPONSE_DICT[echo_id] = response
    _RESPONSE_TIME_DICT[echo_id] = time.time()


async def check_timeout_response():
    """后台清理超时响应 (防内存泄漏)"""
    while True:
        try:
            now_time = time.time()
            for echo_id, t0 in list(_RESPONSE_TIME_DICT.items()):
                if now_time - t0 > HEARTBEAT_INTERVAL:
                    _RESPONSE_DICT.pop(echo_id, None)
                    _RESPONSE_TIME_DICT.pop(echo_id, None)
                    print(f"[qq_bridge] 响应 {echo_id} 超时已清理", flush=True)
        except Exception:
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL)


class QQBridge:
    # 自主回应(不@)频率控制: 冷却300s(5分钟) + 每小时最多3次
    AUTO_COOLDOWN = 300
    AUTO_MAX_PER_HOUR = 3
    # 内心分享(好奇/念头)冷却: 10分钟一次 (主动发言整体预算另控)
    SHARE_COOLDOWN = 600
    # ★ 僵尸连接防护: 静默30分钟无事件 或 连接超3小时 → 强制重连
    WS_SILENCE_RECONNECT = 1800
    WS_MAX_AGE = 3 * 3600

    def __init__(self, cfg, respond_fn, kiri=None):
        self.cfg = cfg
        self.respond = respond_fn          # respond(text, user, scene) → str
        self.kiri = kiri                   # ★ 核心实例 (mood/share_queue来源, 避免import app循环)
        self.ws = None
        self.q = asyncio.Queue()           # 消息事件 (单worker串行回复)
        self.proactive_q = queue.Queue()   # 主动发言队列 (跨线程)
        self._worker_task = None
        self._auto_times = []              # 自主回应时间戳 (频率控制)
        self.group_history = {}            # group_id → 最近群消息 [(who, text)]
        self.group_ids = set()             # 见过的群 (内心分享发到最近活跃群)
        self._group_last = {}              # group_id → 最后活跃时间
        self._last_share = 0.0             # 上次内心分享时间
        self.last_recv_ts = 0.0            # ★ 最后收到ws事件时间 (静默看门狗/监控用)
        self._conn_start = 0.0
        # ★ 账号在线检测 (2026-08-19): 踢号后 NapCat WS 心跳照发, 静默看门狗测不出
        #   get_status 的 online=false 才是真相 → 记录 + 监控显示
        self.online = True                 # 最近一次账号在线状态
        self.online_since = time.time()    # 当前状态起始时间
        self._last_private_chat = None     # ★ 最近活跃私聊 (重连通知用, 2026-08-27)
        self._online_check_ts = 0.0
        # ★ 掉线守护 (2026-08-22): 掉线期间发送不丢弃 → pending 补发队列 (限50条带时间戳)
        self._pending_sends = []           # [(ts, kind, action, params, echo)]
        self._down_since = 0.0             # 本次掉线起始时间 (0=在线)
        self._load_groups()                # ★ 恢复已知群 (重启后仍知道往哪发)
        # ★ 2026-08-21 移植 NachoBot:
        #   echo 响应池 (api_call 带回执 — 拿群名/成员信息/被引用消息原文)
        #   全局发送队列 (串行 + 随机限速防风控)
        self.send_q = asyncio.Queue()      # ("_send"/"_api", action, params, echo)
        self._heartbeat_ts = 0.0           # 上次心跳时间 (判死链)
        # ★ 2026-08-22 多轮输入合并 (真人连发多条 → 合并一轮): 防抖窗口
        self._batch = {}                   # (mtype,user,group) -> list[event]
        self._batch_tasks = {}             # key -> task
        # ★ 2026-08-22 移植 NachoBot: Bot 被禁言切链路 (省 token)
        self.muted_until = {}              # group_id -> 禁言到期时间戳
        self._last_poke = 0.0              # 上次回 poke 时间 (限频)

    # ---------- 已知群持久化 (重启不丢, 分享才知道往哪发) ----------
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

    # ---------- 发送 ----------
    async def _send(self, action, params):
        """低层发送 (直接写ws, 无回执, 仅内部紧急用; 常规走 send_q)"""
        if not self.ws:
            return
        await self.ws.send(json.dumps(
            {"action": action, "params": params}, ensure_ascii=False))

    async def api_call(self, action, params, timeout=10):
        """★ 带 echo 回执的 API 调用 (2026-08-21 移植 NachoBot nc_sending.send_message_to_napcat)
        塞入发送队列(共用限速节奏) → get_response 轮询等回执
        返回响应 dict (含 data) 或 None (超时/失败/ws未就绪)"""
        if not self.ws:
            return None
        import uuid
        request_uuid = str(uuid.uuid4())
        try:
            await self.send_q.put(("_api", action, params, request_uuid))
            return await get_response(request_uuid, timeout)
        except Exception:
            return None

    async def send_private(self, user_id, text):
        await self.send_q.put(("_send", "send_private_msg", {"user_id": int(user_id), "message": text}, None))

    async def send_group(self, group_id, text):
        await self.send_q.put(("_send", "send_group_msg", {"group_id": int(group_id), "message": text}, None))

    async def _sender_loop(self):
        """★ 全局发送队列 worker (2026-08-21 NachoBot吸收: nc_sending 随机限速)
        串行发送 (主动/回复/分享共用节奏, 不互相叠加打爆) + 随机抖动防风控
        _api 项 = api_call (带 echo, 发完 resolve Future); _send 项 = 普通消息"""
        import random
        while True:
            try:
                kind, action, params, echo = await self.send_q.get()
                if not self.ws:
                    # ★ 掉线守护 (2026-08-22): 掉线时不丢弃 → 进 pending, 重连后限时限量补发
                    if len(self._pending_sends) < 50:
                        self._pending_sends.append((time.time(), kind, action, params, echo))
                    if self._down_since == 0:
                        self._down_since = time.time()
                    self.send_q.task_done()
                    await asyncio.sleep(1)
                    continue
                payload = {"action": action, "params": params}
                if echo:
                    payload["echo"] = echo
                try:
                    await self.ws.send(json.dumps(payload, ensure_ascii=False))
                except Exception as exc:
                    self._log_bridge(f"发送失败 {action}: {exc}")
                self.send_q.task_done()
                # ★ 随机限速: 1.5~3s 抖动 (NachoBot nc_sending: 防风控)
                await asyncio.sleep(1.5 + random.uniform(0.5, 1.5))
            except Exception:
                await asyncio.sleep(0.5)

    async def _flush_pending(self):
        """★ 掉线守护补发 (2026-08-22): 重连成功后补发积压消息
        限时限量防刷屏: 掉线超过2小时的消息丢弃; 最多补发 10 条"""
        if not self._pending_sends:
            self._down_since = 0.0
            return
        now = time.time()
        total = len(self._pending_sends)
        items = []
        while self._pending_sends:
            ts, kind, action, params, echo = self._pending_sends.pop(0)
            if now - ts > 7200:      # 掉线 >2h 的消息过时, 丢弃
                continue
            items.append((kind, action, params, echo))
        self._down_since = 0.0
        if not items:
            self._log_bridge("补发: 积压 %d 条全部过期, 丢弃" % total)
            return
        self._log_bridge(f"补发: 重连成功, 补发 {len(items)} 条 (限前10)")
        import random
        for item in items[:10]:
            await self.send_q.put(item)
            await asyncio.sleep(1.5 + random.uniform(0.5, 1.5))

    async def _send_chunked(self, target_type, target_id, text, gap=1.5):
        """回复分小段连发 (治'一段长台词'机械感)
        ★ 2026-08-21 雾弥: 模型可用 <sep> 显式分段 — 想连发几条就写几条
        (真人式连发: '行吧，那我去了。<sep>查到啥了我回来告诉你。')
        <sep> 优先; 没有 <sep> 才按句末标点硬拆 (旧规则兜底)"""
        t = str(text or "")
        if "<sep>" in t:
            chunks = [c.strip() for c in t.split("<sep>") if c.strip()]
        else:
            chunks = _split_sentences(t)
        if len(chunks) <= 1:
            if target_type == "private":
                await self.send_private(target_id, t)
            else:
                await self.send_group(target_id, t)
            return
        for i, c in enumerate(chunks):
            if target_type == "private":
                await self.send_private(target_id, c)
            else:
                await self.send_group(target_id, c)
            if i < len(chunks) - 1:
                await asyncio.sleep(gap)

    # ---------- 消息处理 ----------
    MESSAGE_DEBOUNCE = 2.5   # ★ 多轮输入合并窗口 (真人连发多条 → 合并一轮)

    async def _parse_message(self, msg):
        """★ 2026-08-22 移植 NachoBot message_handler: 完整消息段解析
        text/at/face/image/reply/forward/json/record/video → 文本
        (reply/forward 走 echo 池 api_call 拿原文)"""
        if isinstance(msg, str):
            return extract_text(msg, self.cfg.get("bot_qq", ""))
        parts = []
        for seg in msg or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type", "")
            data = seg.get("data", {}) or {}
            if stype == "text":
                parts.append(data.get("text", ""))
            elif stype == "at":
                q = str(data.get("qq", ""))
                bot = str(self.cfg.get("bot_qq", ""))
                if q == "all":
                    parts.append("@所有人 ")
                elif bot and q == bot:
                    parts.append("@Kiri ")
                elif q:
                    parts.append(f"@{q} ")
            elif stype == "face":
                parts.append(self._face_text(data.get("id", "")))
            elif stype == "image":
                parts.append("[图片]")
            elif stype == "record":
                parts.append("[语音]")
            elif stype == "video":
                parts.append("[视频]")
            elif stype == "reply":
                rt = await self._reply_text(data.get("id", ""))
                if rt:
                    parts.append(rt)
            elif stype == "forward":
                ft = await self._forward_text(data.get("id", ""))
                if ft:
                    parts.append(ft)
            elif stype == "json":
                try:
                    import card_parser
                    parts.append(card_parser.parse_json_card(seg))
                except Exception:
                    parts.append("[卡片]")
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def _face_text(self, face_id):
        """★ 移植 NachoBot qq_emoji_list: 表情 id → 文本"""
        try:
            from qq_emoji_list import qq_face
            return qq_face.get(str(face_id), f"[表情:{face_id}]") + " "
        except Exception:
            return f"[表情:{face_id}] "

    async def _reply_text(self, message_id):
        """★ 移植 NachoBot handle_reply_message: get_msg 拿被引用消息原文"""
        try:
            detail = await self.api_call("get_msg", {"message_id": int(message_id)})
            if not detail:
                return ""
            data = detail.get("data") or {}
            sender = data.get("sender") or {}
            nick = sender.get("nickname") or "未知用户"
            uid = sender.get("user_id") or "?"
            orig = await self._parse_message(data.get("message"))
            if not orig:
                orig = "(无内容)"
            return f"[回复<{nick}:{uid}>：{orig[:80]}]，说："
        except Exception:
            return ""

    async def _forward_text(self, message_id):
        """★ 移植 NachoBot handle_forward_message: 递归展开, 图片≥5转占位符"""
        try:
            data = await self.api_call("get_forward_msg", {"id": str(message_id)})
            if not data:
                return "[合并转发]"
            messages = (data.get("data") or {}).get("messages") or []
            text, img_count = await self._walk_forward(messages, 0)
            return text or "[合并转发]"
        except Exception:
            return "[合并转发]"

    async def _walk_forward(self, messages, layer):
        """递归展开转发消息 → (文本, 图片数)"""
        texts, img = [], 0
        for m in messages or []:
            name = (m.get("sender") or {}).get("nickname") or "?"
            content = m.get("content") or m.get("message") or []
            if isinstance(content, str):
                texts.append(f"[{name}] {content[:100]}")
                continue
            inner = ""
            for seg in content if isinstance(content, list) else []:
                if not isinstance(seg, dict):
                    continue
                st = seg.get("type", "")
                d = seg.get("data", {}) or {}
                if st == "text":
                    inner += d.get("text", "")
                elif st == "image":
                    img += 1
                    inner += "[图片]" if img < 5 else "[图片]"
                elif st == "face":
                    inner += self._face_text(d.get("id", ""))
                elif st == "json":
                    try:
                        import card_parser
                        inner += card_parser.parse_json_card(seg)
                    except Exception:
                        pass
            texts.append(f"[{name}] {inner[:150]}")
        return ("\n".join(texts), img)

    def map_user(self, user_id):
        """QQ号 → Kiri的用户名: 雾弥本人→雾弥(恋人线), 其他→QQ号(陌生人起步)"""
        if str(user_id) == str(self.cfg.get("owner_qq", "")):
            return "雾弥"
        return str(user_id)

    def _display_name(self, event):
        """群消息显示名: 名片/昵称 → 雾弥/名字 (给模型看, 治'分不清跟谁讲话')
        记忆身份仍用 QQ号 (map_user), 这里只改显示"""
        uid = str(event.get("user_id", ""))
        if str(uid) == str(self.cfg.get("owner_qq", "")):
            return "雾弥"
        sender = event.get("sender") or {}
        card = (sender.get("card") or "").strip()
        nick = (sender.get("nickname") or "").strip()
        return (card or nick or uid)[:20]

    async def _handle(self, event):
        mtype = event.get("message_type")
        if mtype not in ("private", "group"):
            return
        user_id = str(event.get("user_id", ""))
        if not user_id:
            return
        # 忽略机器人自己发的 (防自答循环)
        if str(user_id) == str(self.cfg.get("bot_qq", "")):
            return
        # ★ 记录最近活跃私聊 (重连通知用, 2026-08-27)
        if mtype == "private":
            self._last_private_chat = user_id
        # ★ 群禁言切链路 (2026-08-22 移植 NachoBot: 被禁言不烧 token 回复)
        if mtype == "group":
            gid = str(event.get("group_id", ""))
            if self.muted_until.get(gid, 0) > time.time():
                return
        # ★ 多轮输入合并 (2026-08-22 雾弥: 真人连发多条 → 合并一轮)
        #   同会话消息进批, 防抖窗口后合并处理 (批内多条消息拼成一段)
        key = (mtype, user_id, str(event.get("group_id", "")))
        self._batch.setdefault(key, []).append(event)
        if key not in self._batch_tasks:
            self._batch_tasks[key] = asyncio.create_task(self._batch_process(key))

    async def _batch_process(self, key):
        """★ 多轮合并处理: 防抖窗口结束 → 合并批内消息 → 单轮 respond"""
        await asyncio.sleep(self.MESSAGE_DEBOUNCE)
        events = self._batch.pop(key, [])
        self._batch_tasks.pop(key, None)
        if not events:
            return
        mtype, user_id, group_id = key
        # 合并文本 (批内多条消息 → 换行分隔)
        texts = []
        for ev in events:
            t = await self._parse_message(ev.get("message"))
            if t:
                texts.append(t)
        if not texts:
            return
        text = "\n".join(texts)
        event = events[0]   # 用第一条的元数据

        if mtype == "group":
            if group_id:
                self.group_ids.add(group_id)
                self._group_last[group_id] = time.time()
                self._save_groups()
                # 群历史: 每条消息分别记 (显示名 + 文本)
                for ev, t in zip(events, texts):
                    self.group_history.setdefault(group_id, []).append(
                        (self._display_name(ev), t))
                self.group_history[group_id] = self.group_history[group_id][-8:]
            target_user = self.map_user(user_id)
            ctx = self._group_context(group_id, 5)
            if any(self._mentioned(ev) for ev in events):
                # 被@ → 必回 (批内任一消息@了她)
                reply = await self._safe_respond(text, target_user, scene="group", group_context=ctx)
                await self._send_chunked("group", group_id, reply)
                self._append_kiri_said(group_id, reply)
            else:
                # 没@ → 自主回应判断
                asyncio.create_task(self._auto_respond(event, group_id, text))
        else:
            target_user = self.map_user(user_id)
            reply = await self._safe_respond(text, target_user)
            await self._send_chunked("private", user_id, reply)

    async def _handle_notice(self, event):
        """★ 2026-08-22 移植 NachoBot notice_handler: 事件分发 (poke/禁言)"""
        try:
            ntype = event.get("notice_type")
            if ntype == "notify" and event.get("sub_type") == "poke":
                await self._handle_poke(event)
            elif ntype == "group_ban":
                await self._handle_group_ban(event)
        except Exception:
            pass

    async def _handle_poke(self, event):
        """★ 移植 NachoBot poke (notice_handler.py:203-271): 被戳 → 回一句 (10分钟限频)"""
        target = str(event.get("target_id", ""))
        if target and target != str(self.cfg.get("bot_qq", "")):
            return  # 不是戳她
        now = time.time()
        if now - self._last_poke < 600:
            return
        self._last_poke = now
        import random
        user_id = str(event.get("user_id", ""))
        group_id = str(event.get("group_id", ""))
        if group_id:
            replies = ["（耳朵动了动）谁戳我？", "别戳了别戳了，痒！", "（尾巴一甩）干嘛呀，本喵正忙着呢"]
            await self._send_chunked("group", group_id, random.choice(replies))
        else:
            replies = ["（耳朵动了动）你戳我干嘛~", "痒！再戳我就挠你", "哼，戳一下要收费的"]
            await self._send_chunked("private", user_id, random.choice(replies))

    async def _handle_group_ban(self, event):
        """★ 移植 NachoBot 禁言切链路 (notice_handler.py:319-327): Bot被禁言→记录到期, 禁言期不回复"""
        group_id = str(event.get("group_id", ""))
        if not group_id:
            return
        duration = int(event.get("duration") or 0)
        if duration > 0:
            self.muted_until[group_id] = time.time() + duration
            self._log_bridge(f"群{group_id} 被禁言 {duration}s, 禁言期不回复")
        else:
            self.muted_until.pop(group_id, None)
            self._log_bridge(f"群{group_id} 解禁, 恢复回复")

    def _mentioned(self, event):
        """群消息里是否@了机器人"""
        bot = str(self.cfg.get("bot_qq", ""))
        if not bot:
            return False
        return bot in extract_at_qq(event.get("message", []))

    # ---------- 自主回应 (不@, 本地LLM两级判断) ----------
    def _auto_allowed(self):
        """频率控制: 冷却期内不发; 每小时上限"""
        now = time.time()
        if self._auto_times and now - self._auto_times[-1] < self.AUTO_COOLDOWN:
            return False
        cutoff = now - 3600
        self._auto_times = [t for t in self._auto_times if t > cutoff]
        return len(self._auto_times) < self.AUTO_MAX_PER_HOUR

    def _mood_text(self):
        """她当前心情 → 文本 (给本地gate用)"""
        try:
            e = self.kiri.state.emotion.state
            mood = e["deep_affect"]["current_mood"]
            if mood > 0.3:
                return "不错"
            if mood < -0.3:
                return "低落"
            return "平静"
        except Exception:
            return "平静"

    def _group_context(self, group_id, n=4):
        """最近n条群消息 (给她看"群里在聊什么", 含她自己说的)"""
        hist = self.group_history.get(group_id, [])[-n:]
        return "\n".join(f"{who}: {txt}" for who, txt in hist)

    def _append_kiri_said(self, group_id, text):
        """她说的话记入群历史 — 主动发言/分享也算'她自己讲话的范畴'"""
        try:
            if not group_id or not text:
                return
            self.group_history.setdefault(group_id, []).append(("Kiri", text[:80]))
            self.group_history[group_id] = self.group_history[group_id][-8:]
        except Exception:
            pass

    async def _auto_respond(self, event, group_id, text=None):
        """没@的群消息: gate1(本地:值不值得) → API生成 → gate2(本地:发不发)
        潜水也留痕: 不参与/想了想没说/自主接话 都记心路
        text: 2026-08-22 多轮合并后的文本 (None 则自己解析)"""
        try:
            if not self._auto_allowed():
                return
            user_id = str(event.get("user_id", ""))
            if text is None:
                text = extract_text(event.get("message", ""), self.cfg.get("bot_qq", ""))
            if not text or len(text) < 2:
                return

            import local_llm
            import kiri_mind
            ctx = self._group_context(group_id, 5)
            mood_txt = self._mood_text()

            # gate1: 本地判断值不值得参与
            join, why = await asyncio.to_thread(local_llm.should_join, ctx, mood_txt)
            if not join:
                kiri_mind.heard(text, f"潜水没插话({str(why)[:20]})", user=user_id)
                return

            # API 生成回应 (她的人格/记忆/情绪, 群聊场合规则 + 完整群上下文)
            target_user = self.map_user(user_id)
            reply = await self._safe_respond(text, target_user, scene="group", group_context=ctx)

            # gate2: 本地判断发不发 (防冷场/太私密/心情差)
            send, why2 = await asyncio.to_thread(local_llm.should_send, reply, ctx, mood_txt)
            if not send:
                kiri_mind.heard(text, f"想了想没说({str(why2)[:20]})", user=user_id)
                return

            await self._send_chunked("group", group_id, reply)
            self._append_kiri_said(group_id, reply)   # ★ 她说的话进群历史
            self._auto_times.append(time.time())
            kiri_mind.heard(text, "自主接话", user=user_id)
            print(f"[qq_bridge] 自主接话: {reply[:30]}", flush=True)
        except Exception as exc:
            print(f"[qq_bridge] 自主回应出错: {exc}", flush=True)

    # ---------- 内心分享: 好奇/念头想说出来 → 审查 → 发最近活跃群 ----------
    def _share_allowed(self):
        """睡眠期严格禁止; 分享冷却30分钟
        2026-08-20 放宽: 2-7深睡才禁, 23-2允许分享 (深夜想起什么也能说)"""
        try:
            if self.kiri and self.kiri.state.is_deep_sleeping():
                return False
        except Exception:
            pass
        return time.time() - self._last_share >= self.SHARE_COOLDOWN

    def _take_share(self):
        """从 kiri.share_queue 取一条想说的话 (非阻塞)"""
        try:
            if self.kiri:
                return self.kiri.share_queue.get_nowait()
            return None
        except queue.Empty:
            return None
        except Exception:
            return None

    async def _do_share(self, share):
        """审查 → 发到最近活跃的群"""
        if not self.group_ids:
            return  # 还没见过任何群
        group_id = max(self.group_ids, key=lambda g: self._group_last.get(g, 0))
        ctx = self._group_context(group_id, 4)
        import local_llm
        import kiri_mind
        ok, why = await asyncio.to_thread(local_llm.review_share, share["text"], ctx)
        if not ok:
            kiri_mind.heard(share["text"], f"想了想没说({str(why)[:20]})", user="群")
            return
        await self.send_group(group_id, share["text"])
        self._append_kiri_said(group_id, share["text"])   # ★ 分享的话也算她说的
        self._last_share = time.time()
        kiri_mind.heard(share["text"], f"分享到群里({share.get('kind','')})", user="群")
        print(f"[qq_bridge] 内心分享[{share.get('kind')}]: {share['text'][:30]}", flush=True)

    async def _share_loop(self):
        while True:
            try:
                if self._share_allowed():
                    share = self._take_share()
                    if share:
                        await self._do_share(share)
            except Exception as exc:
                print(f"[qq_bridge] 分享循环出错: {exc}", flush=True)
            await asyncio.sleep(30)

    # ---------- 主动发言转发 (雾弥的QQ) ----------
    def enqueue_proactive(self, say):
        owner = str(self.cfg.get("owner_qq", ""))
        if owner and say:
            self.proactive_q.put(say)

    async def _proactive_loop(self):
        while True:
            # ★ ws 未就绪时不消费队列 (否则连接建立前入队的主动发言会丢)
            if not self.ws:
                await asyncio.sleep(1)
                continue
            try:
                say = self.proactive_q.get_nowait()
                # ★ 连发 (治机械感): 多行=多条消息逐条发(行间3秒), 去掉"[她主动找你]"系统前缀
                lines = [l.strip() for l in str(say).splitlines() if l.strip()]
                if not lines:
                    continue
                for i, line in enumerate(lines):
                    await self.send_private(self.cfg["owner_qq"], line)
                    if i < len(lines) - 1:
                        await asyncio.sleep(1.5)
                print(f"[qq_bridge] 主动转发({len(lines)}条): {lines[0][:20]}", flush=True)
            except queue.Empty:
                pass  # ★ 必须先于 except Exception: queue.Empty 是 Exception 子类, 反了会被 Exception 吞掉, 每2秒刷"失败:"空日志
            except Exception as exc:
                print(f"[qq_bridge] 主动转发失败: {exc}", flush=True)
            await asyncio.sleep(2)

    # ---------- 主循环 ----------
    async def run(self):
        self._worker_task = asyncio.create_task(self._worker())
        asyncio.create_task(self._proactive_loop())
        asyncio.create_task(self._share_loop())
        asyncio.create_task(self._sender_loop())          # ★ 全局发送队列 (NachoBot移植)
        asyncio.create_task(check_timeout_response())     # ★ echo 响应池超时清理 (NachoBot移植)
        url = self.cfg.get("ws_url", "ws://127.0.0.1:3001")
        print(f"[qq_bridge] 连接 NapCat: {url} (owner_qq={self.cfg.get('owner_qq')})", flush=True)
        while True:
            try:
                async with websockets.connect(url) as ws:
                    self.ws = ws
                    self.last_recv_ts = time.time()   # ★ 最后收到事件时间 (静默检测)
                    self._conn_start = time.time()
                    print("[qq_bridge] [OK] 已连接", flush=True)
                    # ★ 掉线守护 (2026-08-22): 重连成功 → 补发掉线期间积压的消息
                    asyncio.create_task(self._flush_pending())
                    # ★ 静默看门狗: 治 NapCat 僵尸连接 (TCP通/写通/但事件不推 → Kiri收不到消息)
                    #   30分钟没收到任何事件 或 连接超3小时 → 强制关ws重连
                    asyncio.create_task(self._ws_watchdog(ws))
                    # ★ 启动时问 NapCat: 我加入了哪些群 (持久化, 重启后分享有地方发)
                    try:
                        gl = await self.api_call("get_group_list", {})
                        if gl and isinstance(gl.get("data"), list):
                            for g in gl["data"]:
                                gid = str(g.get("group_id", ""))
                                if gid:
                                    self.group_ids.add(gid)
                                    self._group_last[gid] = time.time()
                            if gl["data"]:
                                self._save_groups()
                                print(f"[qq_bridge] 已知群: {len(self.group_ids)}个", flush=True)
                    except Exception:
                        pass
                    async for raw in ws:
                        self.last_recv_ts = time.time()
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        # ★ echo 响应 → 响应池 (2026-08-21 移植 NachoBot: put_response)
                        if event.get("echo"):
                            await put_response(event)
                            continue
                        # ★ heartbeat 元事件 → 秒级踢号感知 (2026-08-21 NachoBot吸收:
                        #   替代60s get_status轮询 — 零额外连接, 离线判定秒级)
                        if event.get("post_type") == "meta_event" and event.get("meta_event_type") == "heartbeat":
                            self._heartbeat_ts = time.time()
                            st = event.get("status") or {}
                            if "online" in st or "good" in st:
                                online = bool(st.get("online", st.get("good", True)))
                                self._on_online_changed(online)
                            continue
                        if event.get("post_type") == "message":
                            self.q.put_nowait(event)
                        elif event.get("post_type") == "notice":
                            # ★ 2026-08-22 移植 NachoBot notice_handler: poke/禁言
                            asyncio.create_task(self._handle_notice(event))
                        elif isinstance(event.get("data"), list):
                            # 无 echo 的 data list 响应 (兼容旧 NapCat) → 群列表
                            for g in event["data"]:
                                gid = str(g.get("group_id", ""))
                                if gid:
                                    self.group_ids.add(gid)
                                    self._group_last[gid] = time.time()
                            if event["data"]:
                                self._save_groups()
                                print(f"[qq_bridge] 已知群: {len(self.group_ids)}个", flush=True)
            except Exception as exc:
                print(f"[qq_bridge] 连接断开: {exc}, 5秒后重连...", flush=True)
            self.ws = None
            await asyncio.sleep(5)

    async def _ws_watchdog(self, ws):
        """静默看门狗 + 账号在线检测: 每30秒查一次
        ①连接静默(30min无事件)或超龄(3h) → 强制重连 (僵尸连接)
        ②每60秒查一次 get_status: online=false → 账号被踢/掉线 (WS心跳照发, 静默测不出)"""
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                silent = now - self.last_recv_ts
                age = now - self._conn_start
                if silent > self.WS_SILENCE_RECONNECT or age > self.WS_MAX_AGE:
                    self._log_bridge(f"ws看门狗: 静默{int(silent)}s/连接{int(age)}s → 强制重连")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                # ★ 账号在线检测兜底 (每300s, 2026-08-21 降频: heartbeat 已秒级感知, 这个只在缺失时用)
                if now - self._online_check_ts >= 300:
                    self._online_check_ts = now
                    await self._check_online()
            except Exception:
                return

    async def _check_online(self):
        """查 get_status: online=false 说明账号掉线/被踢 (WS连接本身正常)
        ★ 2026-08-21: 降为低频兜底 (heartbeat 事件已秒级检测, 这个只在 heartbeat 缺失时用)
        ★ 用独立临时连接查询: 不能与主循环共用ws recv (响应会被主循环抢走, 读到心跳误判在线)
        只记录+告警, 不自动重连 (重连也不解决账号离线, 需重新登录)"""
        try:
            import websockets as _ws
            url = self.cfg.get("ws_url", "ws://127.0.0.1:3001")
            async with _ws.connect(url) as tmp:
                await tmp.send(json.dumps({"action": "get_status", "params": {}, "echo": "kiri_online"}))
                # 连接建立后 NapCat 先推 lifecycle.connect 元事件, 必须跳过等 echo 匹配的响应
                online = None
                try:
                    while online is None:
                        raw = await asyncio.wait_for(tmp.recv(), timeout=5)
                        ev = json.loads(raw)
                        if ev.get("echo") == "kiri_online":
                            data = ev.get("data") or {}
                            online = bool(data.get("online", True))
                except asyncio.TimeoutError:
                    return  # 查询超时, 保持原状态
                except Exception:
                    return
            self._on_online_changed(online)
        except Exception:
            pass

    def _on_online_changed(self, online):
        """★ 在线状态变化统一处理 (2026-08-21 NachoBot吸收: heartbeat秒级 + get_status兜底共用)
        踢号/恢复 → 记录 kick_history + 写 kick_state (guardian 定时重登) + 通知监控层"""
        if online == self.online:
            return
        self.online = online
        self.online_since = time.time()
        if not online:
            self._log_bridge("账号离线! (online=false) — 可能被踢, 需重新登录")
            try:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                with open(os.path.join(BASE, "..", "..", "..", "NapCat", "kick_history.txt"), "a",
                          encoding="utf-8") as f:
                    f.write(f"{ts}  账号离线(检测 online=false)\n")
            except Exception:
                pass
            # ★ 2026-08-27 踢号自动重登: 写状态供 guardian 定时重登;
            #   重登尝试中不覆盖 guardian 的状态机 (只更新在线标志)
            prev = self._kick_state_read()
            if prev.get("status") not in ("relogin_attempted", "need_manual_scan"):
                now_ts = time.time()
                self._kick_state_update(
                    bridge_online=False, status="kicked",
                    kicked_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    kicked_at_ts=now_ts,
                    relogin_at=time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(now_ts + KICK_RELOGIN_COOLDOWN_MIN * 60)),
                    relogin_at_ts=now_ts + KICK_RELOGIN_COOLDOWN_MIN * 60,
                    cooldown_min=KICK_RELOGIN_COOLDOWN_MIN,
                    attempts=int(prev.get("attempts", 0)))
            else:
                self._kick_state_update(bridge_online=False)
        else:
            self._log_bridge("账号恢复在线")
            prev = self._kick_state_read()
            was_kicked = (prev.get("bridge_online") is False
                          or prev.get("status") in ("kicked", "relogin_attempted", "need_manual_scan"))
            self._kick_state_update(
                bridge_online=True, status="online",
                recovered_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            # ★ 重连后通知 (可选): 被踢恢复后向最近私聊补发说明
            if was_kicked and KICK_CATCHUP_TEXT and self._last_private_chat:
                try:
                    asyncio.ensure_future(self.send_private(self._last_private_chat, KICK_CATCHUP_TEXT))
                    self._log_bridge(f"重连通知已发送给 {self._last_private_chat}")
                except Exception as e:
                    self._log_bridge(f"重连通知失败: {e}")

    def _kick_state_read(self):
        try:
            if os.path.exists(KICK_STATE_FILE):
                with open(KICK_STATE_FILE, encoding="utf-8-sig") as f:   # utf-8-sig: 兼容 BOM
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _kick_state_update(self, **patch):
        try:
            st = self._kick_state_read()
            st.update(patch)
            with open(KICK_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    async def _worker(self):
        """单worker串行处理消息; ★ 外层防死 (2026-08-19): 任何异常都不让worker协程死掉
        否则桥重连了但没人消费消息队列 = 静默断联 (asyncio task死掉不随重连复活)"""
        while True:
            try:
                event = await self.q.get()
            except Exception as exc:
                self._log_bridge(f"worker_get_error: {exc}")
                await asyncio.sleep(1)
                continue
            try:
                await self._handle(event)
            except Exception as exc:
                self._log_bridge(f"handle_error: {exc}")
                print(f"[qq_bridge] 处理消息出错: {exc}", flush=True)

    def _log_bridge(self, msg):
        """桥运行日志落盘 (排查静默断联用; 窗口print看不到)"""
        try:
            with open(os.path.join(BASE, "bridge_run.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass

    # ★ 防"掉线"核心 (2026-08-19): respond 卡在 API 读取会堵死单 worker
    #   所有后续消息排队无人处理 = "她不说话了"(账号其实在线)
    RESPOND_TIMEOUT = 60

    async def _safe_respond(self, text, target_user, scene="private", group_context=None):
        """respond 超时保护: 60s 没出结果 → 放弃本轮, 发优雅降级, 不堵 worker"""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.respond, text, target_user, scene, group_context),
                timeout=self.RESPOND_TIMEOUT)
        except asyncio.TimeoutError:
            self._log_bridge(f"respond超时({scene}, {target_user}): {text[:30]}")
            return "……刚才卡了一下，你再说一遍？"
        except Exception as exc:
            self._log_bridge(f"respond异常({scene}, {target_user}): {exc}")
            return f"(出错了: {exc})"


def start(cfg, respond_fn, kiri=None):
    """在 daemon 线程里跑桥; ★ 返回 QQBridge 实例 (之前返回线程导致:
    监控读不到ws + 主动转发enqueue_proactive静默失败 — 主动发言没真正发出去)"""
    bridge = QQBridge(cfg, respond_fn, kiri=kiri)

    def _run():
        # ★ Windows 控制台默认GBK, print含✓等符号会崩 → 强制UTF-8
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        asyncio.run(bridge.run())
    t = threading.Thread(target=_run, daemon=True, name="qq-bridge")
    t.start()
    return bridge



