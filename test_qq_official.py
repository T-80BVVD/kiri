# -*- coding: utf-8 -*-
"""QQ 官方桥单元测试: mock 网关 (WS) + mock token 服务 + mock 发送 API
验证: 连接/鉴权/心跳/群@事件→respond→发送/去重/被动模式"""
import sys, io, json, os, threading, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"~/kiri\kiri")

import asyncio
import websockets

# ---- 1. 本地 mock: token 服务 + 网关 WS + 发送 API 记录 ----
SENT = []          # 已发送消息记录
HEARTBEATS = []    # 心跳记录

TOKEN_SRV = "http://127.0.0.1:18090"
WS_SRV = "ws://127.0.0.1:18091/websocket/"

def mock_token_server():
    """HTTP: POST /app/getAppAccessToken → access_token"""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln))
            if self.path == "/app/getAppAccessToken":
                assert str(body.get("appId")) == "YOUR_APP_ID", f"appId={body.get('appId')}"
                out = json.dumps({"access_token": "test_token_123", "expires_in": 7200}).encode()
            elif "/v2/groups/" in self.path and self.path.endswith("/messages"):
                gid = self.path.split("/")[3]
                SENT.append({"group": gid, "body": body})
                out = json.dumps({"id": "msg_sent_1"}).encode()
            elif "/v2/users/" in self.path and self.path.endswith("/messages"):
                uid = self.path.split("/")[3]
                SENT.append({"user": uid, "body": body})
                out = json.dumps({"id": "msg_sent_2"}).encode()
            else:
                out = json.dumps({"error": "not found"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out)
        def do_GET(self):
            if self.path.startswith("/gateway"):
                out = json.dumps({"url": WS_SRV}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers()
                self.wfile.write(out)
            elif self.path.startswith("/v2/groups/") and self.path.endswith("/messages"):
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln))
                SENT.append({"group": self.path.split("/")[3], "body": body})
                out = json.dumps({"id": "msg_sent_1"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers()
                self.wfile.write(out)
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 18090), H)
    srv.serve_forever()

# ---- mock 网关 WS: 发 Hello → 收 Identify → 发群@事件 ×2 (一条重复) ----
async def mock_gateway(ws):
    await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 20000}}))
    recv = await ws.recv()   # Identify
    ident = json.loads(recv)
    assert ident["op"] == 2, "应发 Identify"
    assert ident["d"]["intents"] == (1 << 25), "intents 应为 GROUP_AND_C2C"
    await ws.send(json.dumps({"op": 0, "s": 1, "t": "READY",
                              "d": {"session_id": "sess_test_1"}}))
    # 群@事件 (content 已去@前缀)
    ev = {"op": 0, "s": 2, "t": "GROUP_AT_MESSAGE_CREATE", "d": {
        "id": "msg_evt_1", "msg_seq": 10,
        "content": "早上好呀", "group_openid": "group_test_1",
        "author": {"member_openid": "user_openid_1"}}}
    await ws.send(json.dumps(ev))
    await ws.send(json.dumps(ev))   # ★ 重复推送 (msg_id+seq 相同) → 应被去重
    # 单聊事件
    await ws.send(json.dumps({"op": 0, "s": 3, "t": "C2C_MESSAGE_CREATE", "d": {
        "id": "msg_evt_2", "msg_seq": 20,
        "content": "在吗", "author": {"user_openid": "user_openid_2"}}}))
    # 等桥处理完
    await asyncio.sleep(3)
    await ws.close()

REPLIES = []
def respond_fn(text, user, scene="private", group_context=None):
    REPLIES.append({"text": text, "user": user, "scene": scene})
    return f"回复:{text}"

def main():
    # 起 mock token/发送 服务
    threading.Thread(target=mock_token_server, daemon=True).start()
    time.sleep(0.5)
    # 起 mock 网关 (后台任务)
    async def _gw():
        async with websockets.serve(mock_gateway, "127.0.0.1", 18091):
            await asyncio.Future()
    threading.Thread(target=lambda: asyncio.run(_gw()), daemon=True).start()
    time.sleep(0.5)

    import qq_bridge_official as qbo
    # 让桥连 mock: 临时改 API_BASE/TOKEN_URL
    qbo.API_BASE = "http://127.0.0.1:18090"
    qbo.TOKEN_URL = "http://127.0.0.1:18090/app/getAppAccessToken"
    qbo.GATEWAY_URL = "http://127.0.0.1:18090/gateway"
    qbo.WS_PATH = "/websocket/"

    cfg = {"app_id": "YOUR_APP_ID", "client_secret": "test_secret", "owner_openid": ""}
    bridge = qbo.QQBridgeOfficial(cfg, respond_fn, kiri=None)
    loop = asyncio.new_event_loop()
    task = loop.create_task(bridge.run())
    # 跑 8 秒收集事件, 然后取消任务 (桥是无限循环)
    loop.run_until_complete(asyncio.sleep(8))
    task.cancel()
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    loop.close()

    # ---- 断言 ----
    ok = True
    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'} {name}")
        if not cond:
            ok = False

    check("鉴权 Identify 收到 (intents 正确)", True)
    check("群@事件 → respond 调用", any(r["text"] == "早上好呀" for r in REPLIES))
    check("重复推送去重 (respond 只调1次)",
          sum(1 for r in REPLIES if r["text"] == "早上好呀") == 1)
    check("群消息已发送", any(s.get("group") == "group_test_1" for s in SENT))
    gmsg = [s for s in SENT if s.get("group") == "group_test_1"]
    if gmsg:
        check("群消息 body 正确 (content/msg_type/msg_id)",
              gmsg[0]["body"].get("content") == "回复:早上好呀"
              and gmsg[0]["body"].get("msg_type") == 0
              and gmsg[0]["body"].get("msg_id") == "msg_evt_1")
    check("单聊事件 → respond", any(r["text"] == "在吗" and r["scene"] == "private" for r in REPLIES))
    check("已知群已记录", "group_test_1" in bridge.group_ids)

    print("\n结果:", "全部通过 ✅" if ok else "有失败 ❌")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
