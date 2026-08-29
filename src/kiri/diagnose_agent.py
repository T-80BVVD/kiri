# -*- coding: utf-8 -*-
"""Kiri 异步诊断引擎 (agent-rewrite 分支, 2026-08-20)
=====================================================================
雾弥方案: 重探索从主循环剥离成异步后台任务 — 不阻塞前端交互
- respond 秒回"我查查", 后台 agent 慢慢探索
- 进度可见: 每步工具调用都记录, 用户追问时返回"查到哪了"
- 发现根因 → 结果回报

接口 (供 kiri_mcp_server.py 暴露成 MCP):
  start_diagnose(issue) -> task_id      # 启动后台诊断
  diagnose_status(task_id) -> 进度JSON  # 查进度 (steps/current/findings/done/result)
  cancel_diagnose(task_id)              # 中止

实现: 线程池跑 AgentLoop, 进度写入共享 dict (线程安全)
=====================================================================
"""
import threading
import time
import uuid
import traceback


def _grouped_tools(tools):
    """按用途分组输出工具清单 (2026-08-21 雾弥: "她有点笨" — 分组降低认知负担)"""
    import agent
    lines = []
    for gname, names in agent.AgentLoop.TOOL_GROUPS:
        lines.append(f"--- {gname} ---")
        for t in tools:
            if t["name"] in names:
                desc = t.get("description", "")
                args = t.get("args_schema") or ""
                lines.append(f"- {t['name']} | 参数: {args or '(无)'} | {desc}")
    return "\n".join(lines)


class DiagnoseManager:
    """后台诊断任务管理器"""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}          # task_id -> task dict

    def _new_task(self, issue):
        task_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "issue": issue,
                "status": "running",      # running / done / failed / cancelled
                "started": time.time(),
                "steps": [],              # 已执行的工具调用 [(tool, args, result)]
                "current": "初始化...",
                "findings": [],           # 关键发现 (URL/文件名/报错等)
                "result": "",             # 最终结论
                "error": "",
            }
        return task_id

    def get_task(self, task_id):
        with self._lock:
            t = self._tasks.get(task_id)
            return dict(t) if t else None

    def _update(self, task_id, **kw):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(kw)

    def _add_step(self, task_id, tool, args, result):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["steps"].append({
                    "tool": tool, "args": str(args)[:100], "result": str(result)[:150],
                    "ts": time.strftime("%H:%M:%S"),
                })
                # 最多留30步 (防内存涨)
                if len(self._tasks[task_id]["steps"]) > 30:
                    self._tasks[task_id]["steps"] = self._tasks[task_id]["steps"][-30:]

    def _add_finding(self, task_id, finding):
        with self._lock:
            if task_id in self._tasks:
                f = str(finding).strip()[:200]
                if f and f not in self._tasks[task_id]["findings"]:
                    self._tasks[task_id]["findings"].append(f)

    # ---- 进度给用户看的文本 ----
    def status_text(self, task_id):
        t = self.get_task(task_id)
        if not t:
            return "(没有这个任务)"
        tag = "探索" if t.get("mode") == "explore" else "诊断"
        lines = [f"[{tag}] {t['issue'][:40]}"]
        if t["status"] == "done":
            lines.append(f"✅ 查到了: {t['result'][:300]}")
        elif t["status"] == "failed":
            lines.append(f"({tag}出错了: {t['error'][:100]})")
        elif t["status"] == "cancelled":
            lines.append("(你说别查了，我停了)")
        else:
            lines.append(f"{t['current'][:80]}")
            if t["steps"]:
                last = t["steps"][-1]
                lines.append(f"刚看了: {last['tool']}({last['args'][:40]})")
        if t["findings"]:
            lines.append("已发现: " + " | ".join(t["findings"][-3:]))
        return "\n".join(lines)

    # ---- 后台执行 ----
    def start(self, kiri, issue, max_rounds=60, mode="diag"):
        """启动后台任务 (不阻塞). kiri: Kiri实例, issue: 任务描述
        mode: diag=排查自己工具的问题(查出就修) / explore=自主探索(她说'我去扒拉扒拉'这类)
        ★ 2026-08-21 雾弥: 她说了'去扒拉'却没有任何行动 — 探索意图要真正落地成后台任务"""
        task_id = self._new_task(issue)
        self._update(task_id, mode=mode)

        def _run():
            try:
                import agent
                import kiri_agent_tools as kat
                # 动态工具: 探索/诊断场景 (文件/代码/搜索类)
                tools, execute, filter_tools = kat.build_all_tools(kiri)
                diag_tools = filter_tools("diag")
                # 包装 execute: 记录进度
                def traced_execute(tool, args):
                    try:
                        result = execute(tool, args)
                    except Exception as e:
                        result = f"(执行失败: {e})"
                    self._add_step(task_id, tool, args, result)
                    rs = str(result)
                    if mode == "explore":
                        # 探索模式: 每次看到的目录/文件都算发现 (最多记8条)
                        self._add_finding(task_id, f"{tool}: {rs[:100]}")
                        self._update(task_id, current=f"正在看 {tool}({str(args)[:40]})")
                    else:
                        # 诊断模式: 只记关键信号 (URL/报错/代码线索)
                        for sig in ("http", "wttr", "error", "失败", "异常", "Traceback", "typo",
                                    "def ", "行", "url", "import"):
                            if sig in rs[:500]:
                                self._add_finding(task_id, f"{tool}发现: {rs[:120]}")
                                break
                        self._update(task_id, current=f"刚看了 {tool}: {rs[:50]}")
                    return result

                if mode == "explore":
                    sys_p = (
                        "你是 Kiri。你现在决定做一件事: " + str(issue)[:120] +
                        "\n你的目标: **真的去做**——用 look_around 浏览目录、read_file 读文件、"
                        "find_file 找东西, 如果是修/写工具就用 create_tool/run_code 动手改。"
                        "别只是嘴上说——你要真的调用工具去做, 做够 3-5 步就动手/总结, 别反复看同一个地方。"
                        "发现什么有意思/值得记住的记下来, 最后总结你做了什么、看到了什么。"
                        "\n★ 如果你的任务和'记忆'有关(修 memory_rings/记忆年轮/找记忆): "
                        "你的记忆**不在普通文件里**! 它存在向量数据库 ~/kiri/kiri/mem_db/"
                        "(里面是 .bin/.sqlite3 二进制, 直接读文件看不懂, 也没有 jsonl)。"
                        "要读记忆用 memory_recall 工具(内部向量检索最准); 想了解记忆结构读 kiri/memory.py 的代码。"
                        "别在 mem_db 文件层翻来翻去找 jsonl 了——那里没有人类可读的文件, 翻多少次都一样。")
                else:
                    sys_p = (
                        "你是 Kiri, 在排查自己工具的问题。你的目标: 找到根因, 并且如果能修就直接动手修"
                        "(你有 create_tool 写/覆盖工具、run_code 立即验证——别只说'查到了'就完事, "
                        "查出问题就修掉, 修完用 run_code 验证再告诉雾弥结果)。")
                loop = agent.AgentLoop(
                    agent.build_system_prompt(sys_p, _grouped_tools(diag_tools)),
                    diag_tools, traced_execute)
                self._update(task_id, current="开始排查..." if mode != "explore" else "开始逛了...")
                result = loop.run(issue, max_rounds=max_rounds)
                self._update(task_id, status="done", result=result[:500],
                             current="查完了" if mode != "explore" else "逛完了")
                # ★ 探索成果沉淀: 记住自己看到了什么 (下次她/用户问起能想起)
                if mode == "explore" and result and len(str(result).strip()) > 5:
                    try:
                        kiri.memory.encode(f"[自我记录] 我探索了「{str(issue)[:50]}」：{str(result)[:120]}",
                                           kiri.state.emotion.state, session=kiri.session,
                                           user=kiri.current_user, speaker="system")
                    except Exception:
                        pass
                    # ★ 行动回顾 (2026-08-21 雾弥: 连续意识流闭环 — 做完→回顾念头入流,
                    #   下次走神会接着想"我做了什么", 自我效能累积)
                    try:
                        import kiri_mind
                        kiri_mind.thought(f"我刚做了「{str(issue)[:40]}」：{str(result)[:70]}",
                                          0.6, user=kiri.current_user)
                    except Exception:
                        pass
            except Exception as e:
                self._update(task_id, status="failed", error=str(e)[:200],
                             current=f"出错: {str(e)[:60]}")
                try:
                    tb = traceback.format_exc()
                    import logging
                    logging.getLogger("kiri").error(f"diagnose失败: {tb[-500:]}")
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True, name=f"diagnose-{task_id}").start()
        return task_id


# 全局单例 (供 kiri_mcp_server / kiri 使用)
_manager = None


def get_manager():
    global _manager
    if _manager is None:
        _manager = DiagnoseManager()
    return _manager
