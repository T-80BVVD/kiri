# -*- coding: utf-8 -*-
"""Kiri QQ 掉线守护 (2026-08-22 雾弥: NapCat 每2-3小时被踢, 进程级自动修复)
=====================================================================
职责 (独立进程, 不依赖 qq_bridge):
  每 N 秒探测 NapCat OneBot WS 端口 (默认 3001)
  + 端口通           → 正常
  + 端口不通&QQ进程在 → NapCat 插件挂/僵尸 → 杀QQ → 快速登录重启 (-q 号)
  + 端口不通&QQ进程无 → 没启动/被踢退出   → 快速登录启动 (-q 号)
  冷却: 重启动作最小间隔 (防反复拉起), 连续失败记录"需人工扫码"

用法:
  python qq_guardian.py            # 循环守护 (自动修复)
  python qq_guardian.py --once     # 单次诊断 (只报告不动作)
  python qq_guardian.py --dry-run  # 循环但只报告不动作
状态文件: qq_guardian_status.json (monitor_server 可读)
日志: qq_guardian.log
=====================================================================
"""
import os
import sys
import io
import json
import time
import socket
import subprocess
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
NAPCAT_DIR = r"D:\NapCat"
NAPCAT_WINBOOT = os.path.join(NAPCAT_DIR, "NapCatWinBootMain.exe")
NAPCAT_HOOK = os.path.join(NAPCAT_DIR, "NapCatWinBootHook.dll")
STATUS_FILE = os.path.join(BASE, "qq_guardian_status.json")
LOG_FILE = os.path.join(BASE, "qq_guardian.log")

QQ_NUM = ""          # bot QQ 号 (与 qq_config.json 一致)
WS_PORT = 3001                 # OneBot 正向 WS 端口
CHECK_INTERVAL = 30            # 探测间隔 (秒)
RESTART_COOLDOWN = 300         # 重启动作最小间隔 (秒)
FAIL_TRIGGER = 2               # 连续失败 N 次才动作 (防抖动)

# ★ 踢号自动重登 (2026-08-27, 配合 qq_bridge 写的 kick_state.json):
#   桥检测到账号被踢(online=false) → 写 kick_state → 本守护在冷却后自动快速登录重连
KICK_STATE_FILE = os.path.join(BASE, "kick_state.json")
KICK_BACKOFF_MIN = [30, 60, 120]   # 连续失败退避: 第1/2/3+ 次失败后等待分钟数
VERIFY_WAIT_S = 240                # 重登尝试后, 等桥确认在线的最长等待 (秒)
KICK_LOG_EVERY = 300               # 等待状态的日志节流 (秒)


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_status(st):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def kick_state_read():
    try:
        if os.path.exists(KICK_STATE_FILE):
            with open(KICK_STATE_FILE, encoding="utf-8-sig") as f:   # utf-8-sig: 兼容 BOM
                return json.load(f)
    except Exception:
        pass
    return {}


def kick_state_write(**patch):
    try:
        st = kick_state_read()
        st.update(patch)
        with open(KICK_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _ts_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def kick_ts_str(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def port_open(host="127.0.0.1", port=WS_PORT, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def qq_alive():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                           capture_output=True, text=True, timeout=10)
        return "QQ.exe" in (r.stdout or "")
    except Exception:
        return False


def find_qq_path():
    """从注册表找 QQ.exe 路径 (与 launcher.bat 一致)"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ")
        val, _ = winreg.QueryValueEx(key, "UninstallString")
        winreg.CloseKey(key)
        d = os.path.dirname(val.strip().strip('"'))
        return os.path.join(d, "QQ.exe")
    except Exception:
        return None


def kill_qq():
    try:
        subprocess.run(["taskkill", "/f", "/im", "QQ.exe"],
                       capture_output=True, timeout=15)
        time.sleep(3)
        return True
    except Exception as e:
        log("[kill] 失败: %s" % e)
        return False


def start_napcat(qq_num=QQ_NUM):
    """快速登录启动: NapCatWinBootMain.exe QQ.exe Hook -q <号> (token 免扫码)"""
    qq = find_qq_path()
    if not qq:
        log("[start] 找不到 QQ 路径, 无法自动启动")
        return False
    if not os.path.exists(NAPCAT_WINBOOT):
        log("[start] NapCatWinBootMain.exe 不存在: %s" % NAPCAT_WINBOOT)
        return False
    try:
        cmd = [NAPCAT_WINBOOT, qq, NAPCAT_HOOK, "-q", qq_num]
        subprocess.Popen(cmd, cwd=NAPCAT_DIR,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log("[start] 已启动 NapCat 快速登录 (-q %s)" % qq_num)
        return True
    except Exception as e:
        log("[start] 启动失败: %s" % e)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单次诊断 (不动作)")
    ap.add_argument("--dry-run", action="store_true", help="循环但只报告不动作")
    ap.add_argument("--qq", default=QQ_NUM)
    args = ap.parse_args()

    fail_streak = 0        # 连续端口不通次数
    last_action = 0.0      # 上次重启动作时间
    last_kick_log = 0.0    # 踢号等待状态日志节流
    log("QQ 掉线守护启动 (端口 %d, QQ %s, 间隔 %ds%s)" % (
        WS_PORT, args.qq, CHECK_INTERVAL, " [DRY-RUN]" if args.dry_run else ""))

    while True:
        ok = port_open()
        alive = qq_alive()
        now = time.time()
        kick = kick_state_read()
        # ★ 踢号状态机: 桥检测到被踢(online=false) → 冷却后自动快速登录重连
        kick_active = (kick.get("status") in ("kicked", "relogin_attempted", "need_manual_scan")
                       and kick.get("bridge_online") is not True)
        if kick_active:
            fail_streak = 0   # 踢号期间不叠加端口失败 (WS 通常还通)
            st = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ws": "OK" if ok else "DOWN",
                  "qq_proc": alive, "kick_status": kick.get("status"),
                  "action": "kick_watch"}
            status = kick.get("status")
            relogin_ts = _ts_float(kick.get("relogin_at_ts"))
            if status == "kicked":
                if now >= relogin_ts and (now - last_action) >= RESTART_COOLDOWN:
                    if args.dry_run:
                        if now - last_kick_log >= KICK_LOG_EVERY:
                            last_kick_log = now
                            log("[踢号] 冷却已到 (dry-run 不动作): 将执行快速登录重连")
                    else:
                        last_action = now
                        attempts = int(kick.get("attempts", 0)) + 1
                        cooldown = KICK_BACKOFF_MIN[min(attempts - 1, len(KICK_BACKOFF_MIN) - 1)]
                        log("[踢号重登] 冷却结束 → 快速登录重连 (第%d次; 失败退避%d分)" % (attempts, cooldown))
                        if alive:
                            kill_qq()
                        started = start_napcat(args.qq)
                        kick_state_write(status="relogin_attempted", attempts=attempts,
                                         last_attempt_at=kick_ts_str(now), last_attempt_at_ts=now,
                                         relogin_at=kick_ts_str(now + cooldown * 60),
                                         relogin_at_ts=now + cooldown * 60,
                                         cooldown_min=cooldown,
                                         action_note=("已发起" if started else "启动失败"))
                else:
                    if now - last_kick_log >= KICK_LOG_EVERY:
                        last_kick_log = now
                        left = max(0, int((relogin_ts - now) / 60))
                        log("[踢号] 冷却中, 预计 %s 自动重连 (剩 %d 分)" % (kick.get("relogin_at", "?"), left))
            elif status == "relogin_attempted":
                last_attempt = _ts_float(kick.get("last_attempt_at_ts"))
                if now - last_attempt > VERIFY_WAIT_S:
                    log("[踢号] 快速登录 %d 秒未确认上线(票据可能失效) → 需人工扫码; 将按退避自动重试" % VERIFY_WAIT_S)
                    kick_state_write(status="need_manual_scan")
                elif now - last_kick_log >= KICK_LOG_EVERY:
                    last_kick_log = now
                    log("[踢号] 重登验证中 (已等 %d 秒)..." % int(now - last_attempt))
            elif status == "need_manual_scan":
                if now >= relogin_ts and (now - last_action) >= RESTART_COOLDOWN:
                    if args.dry_run:
                        if now - last_kick_log >= KICK_LOG_EVERY:
                            last_kick_log = now
                            log("[踢号] 退避到期 (dry-run 不动作): 将再次尝试快速登录")
                    else:
                        last_action = now
                        attempts = int(kick.get("attempts", 0)) + 1
                        cooldown = KICK_BACKOFF_MIN[min(attempts - 1, len(KICK_BACKOFF_MIN) - 1)]
                        log("[踢号重登] 退避到期, 再次尝试快速登录 (第%d次)" % attempts)
                        if alive:
                            kill_qq()
                        start_napcat(args.qq)
                        kick_state_write(status="relogin_attempted", attempts=attempts,
                                         last_attempt_at=kick_ts_str(now), last_attempt_at_ts=now,
                                         relogin_at=kick_ts_str(now + cooldown * 60),
                                         relogin_at_ts=now + cooldown * 60,
                                         cooldown_min=cooldown)
                elif now - last_kick_log >= KICK_LOG_EVERY:
                    last_kick_log = now
                    log("[踢号] ⚠️ 需人工扫码! 下次自动重试 %s" % kick.get("relogin_at", "?"))
        elif ok:
            fail_streak = 0
            st = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ws": "OK", "qq_proc": alive, "action": "none"}
        else:
            fail_streak += 1
            if fail_streak >= FAIL_TRIGGER and not args.dry_run and (now - last_action) >= RESTART_COOLDOWN:
                last_action = now
                if alive:
                    log("[修复] WS 不通但 QQ 进程在 (%d 次连续) → 杀QQ重启" % fail_streak)
                    kill_qq()
                    started = start_napcat(args.qq)
                    action = "restart" if started else "restart_failed"
                else:
                    log("[修复] WS 不通且 QQ 进程不在 (%d 次连续) → 快速登录启动" % fail_streak)
                    started = start_napcat(args.qq)
                    action = "start" if started else "start_failed"
            else:
                action = "watch"
            st = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ws": "DOWN",
                  "qq_proc": alive, "fail_streak": fail_streak, "action": action}
        if (not kick_active) and (fail_streak == 1 or ok or (args.dry_run and not ok)):
            log("状态: ws=%s qq_proc=%s streak=%d action=%s" % (
                "OK" if ok else "DOWN", alive, fail_streak, st.get("action", "none")))
        write_status(st)
        if args.once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
