# -*- coding: utf-8 -*-
"""Kiri 网页版启动器 (无QQ, 无桌面GUI) — 2026-08-27
=====================================================================
用法: python launch_web.py
- 创建 Kiri 实例 + 后台 daemon (联想/主动/记忆巩固)
- 启动监控面板: http://127.0.0.1:8766  (网页聊天: /api/chat)
- ★ 不启动 QQ 桥 (import app 前把 load_config 打补丁成空 owner_qq)
- ★ 不打开桌面窗口/托盘 (不执行 app.main, 只在模块级起 daemon+monitor)
前置: local_serve.py 已在跑 (引擎 ENGINE=local, 端口 8767, v2.2 模型)
=====================================================================
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# ★ 屏蔽 QQ 桥: app.py 模块级读 qq_bridge.load_config() 决定是否连 QQ;
#   这里先替换成返回空 owner_qq 的桩, 让 QQ 桥静默跳过 (不连 NapCat)
try:
    import qq_bridge
    qq_bridge.load_config = lambda: {"ws_url": "", "owner_qq": "", "bot_qq": ""}
    print("[launch_web] QQ 桥已屏蔽 (网页版不连 QQ)", flush=True)
except Exception as e:
    print("[launch_web] 屏蔽 QQ 桥失败(继续): %s" % e, flush=True)

# ★ import app = 创建 kiri + 起 daemon 线程 + 起 MonitorServer(8766); 不执行 GUI main
import app  # noqa: F401

print("=" * 56, flush=True)
print("Kiri 网页版已启动 (无QQ)", flush=True)
print("  对话面板 : http://127.0.0.1:8766  (输入页签, 看决策轨迹)", flush=True)
print("  引擎     : local (local_serve:8767, v2.2 模型)", flush=True)
print("  退出     : 结束本进程", flush=True)
print("=" * 56, flush=True)

try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    print("\nKiri 网页版已停止", flush=True)
