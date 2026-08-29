# -*- coding: utf-8 -*-
"""Kiri 内部模块 Agent 工具集 (agent-rewrite 分支, 2026-08-20)
=====================================================================
把 Kiri 的"自己"打包成 agent 可调用的工具 — MCP 即自我 (雾弥方案2)
她可以主动查询/触发自己的模块, 像用遥控器调试自己

模块 → 工具:
  记忆    → memory_recall / memory_status
  情绪    → emotion_state
  联想    → reverie_trigger
  好奇    → curiosity_trigger
  主动性   → proactive_state
  状态    → state_status
  关系    → relation_state
  话题    → topic_status
  聊天历史 → dialog_status

安全: 全部只读/受控触发, 不提供直接改记忆/情绪的写入 (阶段C再加受控写入)
=====================================================================
"""
import time


def build_internal_tools():
    """内部模块工具清单 (给 LLM 看)"""
    return [
        {"name": "memory_recall", "description": "检索自己的记忆 (记得的事/谁说过什么)",
         "args_schema": "query: 要回忆的内容"},
        {"name": "memory_status", "description": "查记忆库状态 (共多少条/每个用户多少)",
         "args_schema": ""},
        {"name": "memory_store", "description": "把一件事记进记忆 (自己的观察/感想/重要的事; 受控: 只能加不能删)",
         "args_schema": "text: 要记住的内容"},
        {"name": "emotion_state", "description": "查自己当前情绪 (心情/愉悦/无聊/能量)",
         "args_schema": ""},
        {"name": "emotion_adjust", "description": "调整自己情绪 (受控: 每次幅度±0.3内, 模拟'自我调节心情')",
         "args_schema": "delta: -0.3到0.3之间的数"},
        {"name": "state_status", "description": "查自己整体状态 (沉默多久/睡眠/停止)",
         "args_schema": ""},
        {"name": "relation_state", "description": "查和各人的关系阶段 (亲密程度)",
         "args_schema": "user: 谁(可空=默认)"},
        {"name": "dialog_status", "description": "查最近对话上下文 (刚聊过什么)",
         "args_schema": "user: 谁(可空=默认)"},
        {"name": "proactive_state", "description": "查主动性状态 (多久没主动/意愿分)",
         "args_schema": ""},
        {"name": "topic_status", "description": "查话题系统 (最近积累的话题线索)",
         "args_schema": ""},
        {"name": "reverie_trigger", "description": "触发一次联想/走神 (会产生念头)",
         "args_schema": ""},
        {"name": "curiosity_trigger", "description": "触发一次好奇 (想搞懂什么→查→学)",
         "args_schema": ""},
        # ★ 元认知 (2026-08-23 雾弥: LLM=大脑, 其余都是身体; 大脑感知身体哪里出问题)
        {"name": "health_status", "description": "★元认知: 感知自己(身体)的健康状态——查哪个模块/工具/连接出问题了。"
         "连接类(NapCat断/服务没起)=可恢复; 模块类(代码报错)=交给云端API/标记。"
         "调用它就能't意识到我这边哪里出问题了'",
         "args_schema": ""},
        # ★ 目标系统 (2026-08-21 雾弥: 主动性完善 — 治本): 她自己创建/维护目标
        {"name": "goal_create", "description": "创建你的目标 (自己决定想做什么, 记下来免得忘)",
         "args_schema": "goal: 目标描述"},
        {"name": "goal_list", "description": "看你进行中的目标 (有没有记着要做的事)",
         "args_schema": ""},
        {"name": "goal_update", "description": "更新某目标的进度 (做到哪了)",
         "args_schema": "goal_id: 目标id, progress: 进展"},
        {"name": "goal_done", "description": "标记目标完成 (做成的事会被记住)",
         "args_schema": "goal_id: 目标id"},
        {"name": "goal_drop", "description": "放弃某目标 (你自己决定不做了, 不勉强)",
         "args_schema": "goal_id: 目标id"},
    ]


def make_internal_execute(kiri):
    """内部模块工具的执行器 (绑定 kiri 实例)"""
    def execute(tool_name, args):
        k = kiri
        args = args or {}
        try:
            if tool_name == "memory_recall":
                mems = k.memory.retrieve(
                    str(args.get("query", "") or "回忆"), n=3, user=k.current_user,
                    current_mood=k.state.emotion.state["deep_affect"]["current_mood"])
                if not mems:
                    return "(记忆里没有相关的)"
                # ★ 事件绑定情绪 (M1): 检索命中 → 情绪记录 (想起/有收获)
                try:
                    if getattr(k, "emotion_events", None) and k.emotion_events.enabled:
                        k.emotion_events.record(
                            "tool_result", "想起: " + mems[0]["text"][:60],
                            extra={"ok": True, "intensity": 0.4})
                except Exception:
                    pass
                return "\n".join(f"- {m['text'][:100]}" for m in mems[:3])

            if tool_name == "memory_store":
                # ★ 受控写入: 只能加不能删; 只存"自己的观察/感想", 不存用户隐私
                text = str(args.get("text", "")).strip()[:200]
                if not text:
                    return "(要告诉我记什么)"
                k.memory.encode(f"[自我记录] {text}", k.state.emotion.state,
                                session=k.session, user=k.current_user, speaker="system")
                return f"(记住了: {text[:60]})"

            if tool_name == "emotion_adjust":
                # ★ 受控情绪调节: 幅度限制 ±0.3, 防止她乱改自己
                try:
                    delta = float(args.get("delta", 0))
                except (TypeError, ValueError):
                    return "(delta 需要是数字)"
                delta = max(-0.3, min(0.3, delta))   # 边界
                cur = k.state.emotion.state["deep_affect"]["current_mood"]
                new = max(-1.0, min(1.0, cur + delta))
                k.state.emotion.state["deep_affect"]["current_mood"] = new
                return f"(心情 {round(cur,2)} → {round(new,2)}, 调了{delta:+.2f})"

            if tool_name == "memory_status":
                users = k.memory.users()
                parts = [f"共 {len(users)} 个用户的记忆库"]
                for u in users[:8]:
                    try:
                        parts.append(f"  {u}: {k.memory.count(u)} 条")
                    except Exception:
                        pass
                return "\n".join(parts)

            if tool_name == "emotion_state":
                e = k.state.emotion.state
                return (f"心情 {round(e['deep_affect']['current_mood'], 2)}"
                        f" | 愉悦 {round(e['surface_emotion']['pleasure'], 2)}"
                        f" | 无聊 {round(k.state.boredom, 3)}"
                        f" | 能量 {round(e.get('energy', 0), 2)}")

            if tool_name == "state_status":
                silence = int((time.time() - k.state.last_interact) / 60)
                return (f"沉默 {silence} 分钟 | 睡眠中 {k.state.is_sleeping()}"
                        f" | 深睡 {k.state.is_deep_sleeping()}"
                        f" | 停止 {k.state.stopped}"
                        f" | 会话 {k.session}")

            if tool_name == "relation_state":
                u = str(args.get("user") or k.current_user)
                rel = k.state.social.relationships.get(u, {})
                if isinstance(rel, dict):
                    return (f"与 {u}: 亲密 {round(rel.get('intimacy', 0), 2)}"
                            f" 信任 {round(rel.get('trust', 0.3), 2)}"
                            f" 阶段 {k.state.relation_stage(user=u)}")
                return f"与 {u}: (还没建立关系)"

            if tool_name == "dialog_status":
                u = str(args.get("user") or k.current_user)
                dlg = k.get_dialog(u)
                if not dlg:
                    return f"(和{u}还没聊过)"
                return "\n".join(f"- {m['role']}: {m['text'][:60]}" for m in dlg[-6:])

            if tool_name == "proactive_state":
                silence = int((time.time() - k.state.last_interact) / 60)
                is_night = time.localtime().tm_hour >= 23 or time.localtime().tm_hour < 2
                score = k.state.want_score(silence, is_night, k.state.memory_glow)
                return (f"沉默 {silence} 分 | 意愿分 {round(score, 2)}"
                        f" (阈值 {0.18}) | 上次主动 {int((time.time()-k.state.last_proactive)/60)} 分前")

            if tool_name == "topic_status":
                try:
                    sig = k.topic_signals
                    return (f"话题信号 {sig.signal_count} 个 | 素材 {len(sig.materials())} 条"
                            f" | 最近分析 {time.strftime('%H:%M', time.localtime(sig.last_analyze)) if sig.last_analyze else '还没'}")
                except Exception as e:
                    return f"(话题系统: {e})"

            if tool_name == "reverie_trigger":
                # 触发一次联想 (会产生念头/可能好奇)
                k.reverie.run_cycle()
                return "(联想完成, 产生了念头)"

            if tool_name == "curiosity_trigger":
                # 触发一次好奇 (基于最近念头)
                thoughts = k.state.thoughts[-1:] if k.state.thoughts else []
                if thoughts:
                    k.reverie._curiosity(thoughts, user=k.current_user)
                    return "(好奇触发完成, 查了点什么)"
                return "(还没有念头可以好奇)"

            # ★ 目标系统 (2026-08-21): 她自主创建/维护目标
            if tool_name == "goal_create":
                import goals
                return goals.create_goal(str(args.get("goal", "")))
            if tool_name == "goal_list":
                import goals
                return goals.list_goals()
            if tool_name == "goal_update":
                import goals
                return goals.update_goal(str(args.get("goal_id", "")), str(args.get("progress", "")))
            if tool_name == "goal_done":
                import goals
                r = goals.done_goal(str(args.get("goal_id", "")))
                # ★ 事件绑定情绪 (M1): 目标完成 → 满足
                try:
                    if getattr(k, "emotion_events", None) and k.emotion_events.enabled:
                        k.emotion_events.record("goal", "完成目标", cause_ref=str(args.get("goal_id", "")),
                                                extra={"action": "done", "intensity": 0.6})
                except Exception:
                    pass
                return r
            if tool_name == "goal_drop":
                import goals
                r = goals.drop_goal(str(args.get("goal_id", "")))
                # ★ 事件绑定情绪 (M1): 放弃目标 → 失落
                try:
                    if getattr(k, "emotion_events", None) and k.emotion_events.enabled:
                        k.emotion_events.record("goal", "放弃目标", cause_ref=str(args.get("goal_id", "")),
                                                extra={"action": "drop", "intensity": 0.5})
                except Exception:
                    pass
                return r
            if tool_name == "health_status":
                # ★ 元认知: 大脑感知身体状态 (LLM=大脑, 其余模块=身体)
                issues = []
                # 1. 连接类: QQ/NapCat 状态
                try:
                    bridge = getattr(k, "qq_bridge", None) or getattr(k, "bridge", None)
                    if bridge:
                        online = getattr(bridge, "online", True)
                        ws = getattr(bridge, "ws", None)
                        if not online or ws is None:
                            issues.append("连接类: QQ/NapCat 不在线(可能被踢, 可等守护拉起/重试)")
                    else:
                        issues.append("连接类: 没接QQ桥(社交不可用)")
                except Exception as e:
                    issues.append("连接类: 查QQ状态出错(%s)" % str(e)[:40])
                # 2. 模块类: 记忆库
                try:
                    n = k.memory.count(k.current_user)
                    issues.append("记忆库正常(%d条)" % n)
                except Exception as e:
                    issues.append("模块类: 记忆库异常(%s)" % str(e)[:40])
                # 3. 近期元认知记录 (life 的近30分钟出错)
                try:
                    import life
                    m = life.meta_state_text()
                    if m:
                        issues.append("近期: " + m[:90])
                except Exception:
                    pass
                return "身体状态:\n" + "\n".join(issues) if issues else "身体状态: 各部件正常"

            return f"(未知内部工具: {tool_name})"
        except Exception as e:
            return f"(内部工具 {tool_name} 出错: {e})"
    return execute


def build_all_tools(kiri):
    """完整工具集: 内部模块 + 外部 MCP (weather/search/bili/探索/self_discover)
    ★ 动态工具 (借鉴 OpenClaw): 按任务类型筛选, 不全部注入上下文
      - 对话场景: 内部模块 + 轻量外部 (weather/search/bili)
      - 诊断场景: 文件/代码/搜索类 + 内部状态 (不注入娱乐类)"""
    import mcp_client
    internal = build_internal_tools()

    def mcp_execute(tool_name, args):
        """外部 MCP 工具执行 (复用 mcp_client)"""
        return mcp_client.call_tool(tool_name, args or {})

    # 外部工具清单 (与 kiri_mcp_server.py 对齐)
    # ★ 探索引导: 说明里带"实现在 kiri_mcp_server.py", 她要诊断时知道读哪个文件
    external = [
        {"name": "weather", "description": "实时天气或预报 (实现在 kiri/kiri_mcp_server.py)",
         "args_schema": "city: 城市(空=自动), day: 今天/明天/后天"},
        {"name": "search", "description": "Bing 搜索网页资料 (实现在 kiri/kiri_mcp_server.py)",
         "args_schema": "query: 关键词"},
        {"name": "ask_ai", "description": "问一个博学的AI导师 (观点/原理/深度理解)",
         "args_schema": "question: 问题"},
        {"name": "bili_hot", "description": "看B站热门视频", "args_schema": "n: 条数"},
        {"name": "bili_rank", "description": "看B站分区排行榜",
         "args_schema": "zone: 知识/游戏/数码/生活/鬼畜, n: 条数"},
        {"name": "bili_search", "description": "B站搜索视频",
         "args_schema": "keyword: 关键词, n: 条数"},
        {"name": "zhihu_daily", "description": "看知乎日报", "args_schema": "n: 条数"},
        {"name": "sspai_feed", "description": "看少数派文章", "args_schema": "n: 条数"},
        {"name": "look_around", "description": "看文件夹里有什么 (探索电脑/找文件在哪)",
         "args_schema": "path: 路径"},
        {"name": "read_file", "description": "读文件内容 (读代码/文档排查问题; 长文件用offset分段读)",
         "args_schema": "path: 文件路径, offset: 0=从头,1=下一段(可选)"},
        {"name": "find_file", "description": "按文件名找文件。★你的家是 ~/kiri (你的代码都在这); 搜到多个同名的选 alice_v2_qq 的",
         "args_schema": "keyword: 关键词, root: 从哪找(默认~/kiri)"},
        {"name": "grep_file", "description": "在文件里搜关键词(带行号) — 排错利器, 找URL/函数名/报错在哪一行; 你的代码在 kiri/ 目录",
         "args_schema": "keyword: 要找的内容, path: 文件路径"},
        {"name": "check_system", "description": "看电脑状态 (时间/内存/磁盘)", "args_schema": ""},
        {"name": "read_self", "description": "读自己的档案 (我是谁)", "args_schema": ""},
        {"name": "self_discover", "description": "探索自己 (浏览代码/档案了解构成)",
         "args_schema": "focus: 探索重点(可空)"},
        # ★ 创作区 (2026-08-20 雾弥授权): 她自己写MCP工具
        {"name": "create_tool", "description": "写一个你自己的MCP工具(自己动手做东西!) — name=文件名, code=代码(可只写函数体), mode=write(新写/整份覆盖重写同名)/append(追加)。★你的记忆在 ~/kiri/kiri/mem_db/ (UUID文件夹, 里面有jsonl) — 写记忆类工具用这个路径",
         "args_schema": "name: 工具文件名(如 my_ring.py), code: 代码, mode: write/append"},
        {"name": "run_code", "description": "测试你写的代码 — 工具文件(含@mcp.tool)会直接加载并调用第一个函数看结果(不用等重启!), 普通脚本沙箱运行; 报错会显示哪里崩",
         "args_schema": "path: my_creations里的文件, args: 函数参数(如 'query=回忆' 或 json)"},
        {"name": "list_creations", "description": "看你写过的所有工具(创作列表)",
         "args_schema": ""},
        # ★ routerMCP (2026-08-22 雾弥方案): 工具路由器 — 不确定用哪个工具时先问它
        {"name": "route_tool", "description": "工具路由器: 不确定该用哪个工具时调用它。"
         "输入你的需求描述(intent), 它返回该用的工具名+参数(如 intent='查武汉天气' → weather)。"
         "没有合适工具时返回 tool=null — 这时换更具体的说法重新描述, 或直接回复。"
         "★你不需要记所有工具的参数格式, 让 route_tool 帮你选工具和提参数",
         "args_schema": "intent: 用一句自然语言描述你要做什么"},
    ]

    # 合并执行器: 内部工具走 kiri, 外部走 MCP
    internal_exec = make_internal_execute(kiri)
    HOME = r"~/kiri"
    def execute(tool_name, args):
        args = args or {}
        # ★ 默认路径锚定她的家 (防 find_file/look_around 搜到别的项目副本)
        if tool_name == "find_file" and not args.get("root"):
            args = {**args, "root": HOME}
        if tool_name == "look_around" and not args.get("path"):
            args = {**args, "path": HOME}
        if tool_name == "self_discover":
            # ★ self_discover 是 Kiri 的方法, 不走 MCP
            try:
                return kiri._tool_self_discover(str(args.get("focus", "")))
            except Exception as e:
                return f"(自我探索失败: {e})"
        if tool_name == "route_tool":
            # ★ routerMCP (2026-08-22 雾弥): 主进程直接路由 (不起MCP子进程, 省torch/bge重复加载)
            #   模型调 route_tool(intent) → 返回 {tool, args, reason}
            try:
                import router as _router_mod
                import json as _json
                r = _router_mod.ToolRouter(all_tools).resolve_llm(str(args.get("intent", "")))
                return _json.dumps({"tool": r.get("tool"), "args": r.get("args") or {},
                                    "reason": r.get("reason", "")}, ensure_ascii=False)
            except Exception as e:
                return '{"tool": null, "reason": "router错误: %s"}' % str(e)[:80]
        if tool_name in [t["name"] for t in internal]:
            return internal_exec(tool_name, args)
        return mcp_execute(tool_name, args)

    all_tools = internal + external

    def filter_tools(scene="chat"):
        """★ 动态工具列表 (借鉴 OpenClaw): 按场景筛选, 不全部注入
        scene=chat: 日常对话 (内部模块 + 轻量外部)
        scene=diag: 诊断/排错 (文件/代码/搜索 + 内部状态)"""
        if scene == "diag":
            diag_names = {"read_file", "look_around", "find_file", "grep_file", "search",
                          "ask_ai", "check_system", "read_self", "self_discover",
                          "memory_recall", "memory_status", "state_status", "emotion_state",
                          # ★ 创作工具 (2026-08-21): 诊断要能修复 — 查到了就能动手改
                          "create_tool", "run_code", "list_creations"}
            return [t for t in all_tools if t["name"] in diag_names]
        # chat: 全部 (默认)
        return all_tools

    return all_tools, execute, filter_tools
