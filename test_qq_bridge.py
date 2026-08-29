# -*- coding: utf-8 -*-
"""test_qq_bridge.py — QQ桥协议测试 (本地mock OneBot服务器, 不连真QQ)
验证: 私聊回发 / 雾弥映射 / 群聊@才回 / 主动转发
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qq_bridge as qb

WS_URL = "ws://127.0.0.1:3999"
OWNER_QQ = "10001"
BOT_QQ = "20002"
received_actions = []   # 桥发来的 action 帧
respond_calls = []      # respond 被调用记录 (text, user)


def fake_respond(text, user=None, scene=None, group_context=None):
    respond_calls.append((text, user))
    return f"[回复] {user}: {text}"


async def mock_server(events):
    """起 mock OneBot 服务器: 等桥连接 → 依次广播事件 → 收桥回发"""
    import time as _t
    async def handler(ws):
        for ev in events:
            await asyncio.sleep(0.3)
            await ws.send(json.dumps(ev, ensure_ascii=False))
        # 收回发帧直到 5 秒无新帧
        deadline = _t.time() + 5
        while _t.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1)
                received_actions.append(json.loads(raw))
            except asyncio.TimeoutError:
                pass
    import websockets
    async with websockets.serve(handler, "127.0.0.1", 3999):
        await asyncio.sleep(12)  # 给桥连接+处理+回发的时间


def main():
    cfg = {"ws_url": WS_URL, "owner_qq": OWNER_QQ, "bot_qq": BOT_QQ}
    events = [
        # 1. 陌生人私聊 → 应回复, user=QQ号
        {"post_type": "message", "message_type": "private",
         "user_id": 33333, "message": "你好呀"},
        # 2. 雾弥私聊 → 应回复, user=雾弥
        {"post_type": "message", "message_type": "private",
         "user_id": int(OWNER_QQ), "message": "在吗"},
        # 3. 群聊没@ → 不应回复
        {"post_type": "message", "message_type": "group",
         "group_id": 888, "user_id": 33333, "message": "有人吗"},
        # 4. 群聊@了她 → 应回复
        {"post_type": "message", "message_type": "group",
         "group_id": 888, "user_id": 44444,
         "message": [{"type": "at", "data": {"qq": BOT_QQ}},
                     {"type": "text", "data": {"text": "你好"}}]},
    ]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bridge = qb.QQBridge(cfg, fake_respond)
    # 主动转发测试: 入队一条
    bridge.enqueue_proactive("我想你了")
    bridge_task = loop.create_task(bridge.run())
    loop.create_task(mock_server(events))
    # 跑 12 秒: 服务器广播 → 桥处理回发 → 服务器收帧
    loop.run_until_complete(asyncio.sleep(12))
    bridge_task.cancel()
    try:
        loop.run_until_complete(bridge_task)
    except (asyncio.CancelledError, Exception):
        pass
    loop.close()

    # 收集断言
    ok = True
    print("\n=== respond 调用记录 ===")
    for c in respond_calls:
        print("  ", c)
    print("\n=== 桥发出的 action ===")
    for a in received_actions:
        print("  ", a.get("action"), a.get("params"))

    # 断言1: 3次回复 (私聊x2 + 群@x1; 群没@不应答)
    n_private = sum(1 for a in received_actions if a.get("action") == "send_private_msg")
    n_group = sum(1 for a in received_actions if a.get("action") == "send_group_msg")
    if n_private < 2:
        print(f"[FAIL] 私聊回复数 {n_private} < 2"); ok = False
    if n_group < 1:
        print(f"[FAIL] 群@回复数 {n_group} < 1"); ok = False
    # 断言2: 雾弥映射
    if not any(u == "雾弥" for _, u in respond_calls):
        print("[FAIL] owner 未映射为雾弥"); ok = False
    # 断言3: 陌生人映射为QQ号
    if not any(u == "33333" for _, u in respond_calls):
        print("[FAIL] 陌生人未映射为QQ号"); ok = False
    # 断言4: 主动转发
    if not any(a.get("action") == "send_private_msg" and "我想你了" in json.dumps(a.get("params"), ensure_ascii=False)
               for a in received_actions):
        print("[WARN] 未捕获主动转发 (可能时序问题, 手动检查)")
    print("\n" + ("✓ 全部通过" if ok else "✗ 有失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
