# -*- coding: utf-8 -*-
"""Kiri 网页版启动器 (不连 QQ, 无桌面 GUI) — 2026-08-29
=====================================================================
用途: 只想用网页控制台(8766)看 Kiri 聊天/主动消息/回溯, 不想连 QQ 时用这个。

用法:  python launch_web.py
- 创建 Kiri 实例 + 后台 daemon (联想/主动/记忆巩固)
- 启动监控面板: http://127.0.0.1:8766  (网页聊天: /api/chat, 主动消息: /api/proactive)
- ★ 不启动 QQ 桥: import app 前把 qq_bridge_official.load_config 打补丁成返回空配置
- ★ 不打开桌面窗口/托盘: app.py 的 main() 只在 __main__ 执行, import 不会触发

引擎: 与 app.py 一致 (config.ENGINE, 默认 api/DeepSeek)
依赖: 无额外 (monitor_server 是纯标准库 HTTP)
=====================================================================
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "src", "kiri"))
sys.path.insert(0, os.path.join(BASE, "src"))

# ★ 屏蔽 QQ 桥: app.py 模块级读 qq_bridge_official.load_config() 决定是否连 QQ;
#   这里先替换成返回空配置的桩, 让 QQ 桥静默跳过 (网页版不连 QQ)
try:
    import qq_bridge_official
    def _no_qq():
        return {"app_id": "", "client_secret": "", "owner_openid": ""}
    qq_bridge_official.load_config = _no_qq
    print("[launch_web] QQ 桥已屏蔽 (网页版不连 QQ)", flush=True)
except Exception as e:
    print("[launch_web] 屏蔽 QQ 桥失败(继续): %s" % e, flush=True)

# ★ import app = 创建 kiri + 起 daemon 线程 + 起 MonitorServer(8766); 不执行 GUI main
import app  # noqa: F401

print("=" * 56, flush=True)
print("Kiri 网页版已启动 (无QQ)", flush=True)
print("  对话面板 : http://127.0.0.1:8766", flush=True)
print("  (输入页签=聊天, 主动=她主动说的话, 回溯=完整时间线)", flush=True)
print("  退出     : 结束本进程", flush=True)
print("=" * 56, flush=True)

try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    print("\nKiri 网页版已停止", flush=True)
