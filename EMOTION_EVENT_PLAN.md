# Kiri 事件绑定情绪系统 · 实施蓝图（接口与机制冻结版）

> 状态: **冻结稿 (实施契约)** | 2026-08-27 | 配套: `EMOTION_EVENT_DESIGN.md` (Why/What)
> 本文档回答: 改什么 / 接口长什么样 / 新模块在哪 / 顺序 / 怎么验收 / 什么会前功尽弃
> 原则: **先冻结协议再写代码**；接口一经定稿，训练数据生成器与运行时必须共用同一模板

---

## 〇、现状基线（已就位，不许破坏）

| 组件 | 状态 |
|---|---|
| 主模型 local_serve (8767) | ✅ v2.2 14B, 聊天流式 + `<sep>` 分条 + think 保险丝 |
| 网页版 launch_web (8766) | ✅ 无QQ, 聊天优先锁 (CHAT_ACTIVE) |
| 情绪现状 | PAD 标量 (state.emotion) + `_analyze_emotion` (API/主模型) + boredom 标量 |
| 主动性现状 | want_score 标量 ≥ 阈值 → LLM 现编理由 |
| 数据管线 | gen_v22 / merge / cleanup / qc_gate / audit 全链路可用 |

**改造红线**：`state.describe()` 的 PAD 输出格式**不变**（主模型 prompt 依赖它）；红线/防注入/人格不回归。

---

## 一、新增模块（4 个）

| 模块 | 文件 | 职责 |
|---|---|---|
| **情绪记录层** | `emotion_events.py` | EmotionEvent 数据结构 + 落盘 (emotion_events.jsonl) + 缓存 + 聚合计算 + 查询 |
| **事件采集器** | `emotion_events.py` (同模块) | 统一入口 `emotion.record(source, event_text, cause_ref, appraisal)`；各事件源在此注册 |
| **情绪小模型服务** | `emotion_serve.py` (新进程, 端口 8768) | 加载 3B appraiser, 提供 `/appraise`；与 14B local_serve 共存 (3B 4bit ~2GB) |
| **数据蒸馏管线** | `distill_emotion.py` / `train_appraiser.py` | DeepSeek 教师生成 appraisal 对 → QC → QLoRA 训练 → 评估 |

---

## 二、现有模块改动（8 处，逐一冻结改动点）

| 文件 | 改动 | 兼容要求 |
|---|---|---|
| `config.py` | 新增: `EMOTION_EVENTS_ENABLED=True`、`EMOTION_SERVE_URL`、衰减参数、事件保留上限 | 开关可回退到旧行为 |
| `state.py` | ① emotion 数值来源改为 `emotion_events` 聚合 ② boredom 降级为"能量/意愿强度" ③ want_score 只算强度不算原因 ④ `describe()` 输出格式不动 (内部算) | describe 兼容层 |
| `kiri.py` | ① respond 入口采集 user_msg 事件 → appraise → record ② proactive 由头 = 情绪记录/念头事件, prompt 注入 ③ proactive_outcome 闭环 (主动后观察回应) ④ `_analyze_emotion` 可切到小模型 | 开关控制 |
| `reverie.py` | ① 嚼到的记忆 → 产生情绪记录 (温暖/好奇, cause_ref=记忆id) ② `_maybe_act_on_thoughts` salience 加入情绪强度 | 无破坏 |
| `prompt.py` | `proactive_system` 的 [此刻] 注入由头事件 (替代"有点无聊想找他说说话") | 无由头时用旧文案 |
| `goals.py` | 目标完成/放弃/推进 → 情绪记录 | 无破坏 |
| `tools.py` | memory_recall / 工具结果 → 情绪记录 (命中/成败) | 无破坏 |
| `local_serve.py` | 不动 (14B 不变)；小模型走独立 emotion_serve | 无破坏 |

---

## 三、接口协议（冻结版，训练与运行时共用）

### 3.1 情绪小模型接口 `POST /appraise` (emotion_serve:8768)

```jsonc
// 输入 (与训练样本输入完全同构 — 由同一模板生成)
{
  "event_text": "你还记得我说过想看海吗",   // 对方说的话 / 事件描述
  "speaker": "user",                        // user|self|memory|tool|system
  "relationship": "亲密",                   // 亲密|朋友|生疏
  "current_state": {"valence": 0.2, "arousal": 0.4, "intensity": 0.3}  // 可选, 默认中性
}

// 输出 (JSON, temperature 低 ~0.1, 只允许这个 schema)
{
  "valence_delta": -0.3,        // -1..1
  "arousal_delta": 0.2,         // -1..1
  "intensity": 0.6,             // 0..1
  "emotion_tags": ["被冷落"],    // 1~2 个
  "appraisal_note": "他3小时没回，有点失落"   // ≤40字, 给由头 prompt 用
}
```
- 失败/超时 → 返回 `{"error": "..."}`，调用方降级：不产生情绪记录（不阻塞聊天）
- **一致性要求**：同一输入多次推理 delta 方差 < 0.05（temperature 低保证）

### 3.2 EmotionEvent 落盘格式 (emotion_events.jsonl, append-only)

```jsonc
{
  "schema_version": 1,          // 冻结; 以后升级只加字段不删字段
  "id": "ev_001", "ts": 1787795139.8,
  "source": "user_msg",         // user_msg|memory_recall|thought|tool_result|goal|proactive_outcome|time_routine
  "event_text": "他3小时没回消息",
  "cause_ref": "mem_xxx",       // 记忆id/目标id/对话id; 无则 null — "有因可查"的锚点
  "appraisal": {"valence": -0.3, "arousal": 0.2, "relevance": 0.6},
  "emotion_tags": ["被冷落"],
  "intensity": 0.6,
  "decay_hours": 3.0
}
```

### 3.3 统一事件采集入口 (emotion_events.py)

```python
emotion.record(source: str, event_text: str, cause_ref: str|None = None,
               appraisal: dict|None = None, relationship: str|None = None) -> EmotionEvent
# appraisal=None → 调小模型自动评价 (异步: 聊天事件同步2-3s, 后台事件入队)
# 返回 EmotionEvent (含 id) — 调用方可把 id 存进 cause_ref 链
```

### 3.4 心情查询接口 (state.emotion)

```python
state.emotion.current() -> {
  "valence": 0.1, "arousal": 0.3, "mood": 0.1,        # 聚合值 (describe 兼容)
  "contributing": [{"event_text", "intensity", "ts"}], # 贡献事件列表 (有因可查)
}
```

### 3.5 主动性由头接口 (proactive)

```python
emotion.top_candidates(n=3, min_intensity=0.4) -> [EmotionEvent]   # 高情绪强度事件
# prompt 注入 (prompt.py):
#   "[此刻] 你想起 {event_text}。这件事让你{emotion_tags}。{appraisal_note}"
# 无由头 → 不主动 (或降级旧文案, 由雾弥拍板, 见待拍板#2)
```

### 3.6 数据蒸馏格式 (distill_emotion.py)

```
教师输入 (与 3.1 输入同构): {event_text, speaker, relationship, current_state}
教师输出 (与 3.1 输出同构): {valence_delta, arousal_delta, intensity, emotion_tags, appraisal_note}
→ 训练样本 = ChatML(user=输入JSON, assistant=输出JSON)
```

---

## 四、事件源清单（冻结，每个采集点有代码锚点）

| source | 采集点 | 触发 |
|---|---|---|
| user_msg | kiri.respond 入口 | 每条用户消息 |
| memory_recall | tools.py memory_recall 执行后 | 命中/未命中 |
| thought | reverie.run_cycle 嚼到记忆时 | 每次联想 |
| tool_result | 工具执行后 (成败) | 每次调用 |
| goal | goals.py 完成/放弃/推进 | 状态变化 |
| proactive_outcome | 主动后观察对方回应 (聊天优先锁内) | 主动后的下一次消息 |
| time_routine | daemon tick (深夜/节日/周年) | 低频 |

**缺口**：`proactive_outcome` 闭环依赖"她的主动消息真的发出去并被回应"——**网页版目前没有"她主动说"的推送通道**（proactive 只写日志）。这是 M2 的前置依赖（见待拍板#1）。

---

## 五、实施顺序（依赖冻结）+ 每阶段验收

| 阶段 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| **M0** | 本文档冻结；拍板待拍板项 | — | 协议无未决项 |
| **M1 情绪记录层** | emotion_events.py (存储/聚合/查询) + 事件采集 (user_msg/thought/tool/goal 走**规则评价**先跑) + describe 兼容层 | M0 | ①心情数值有因可查 ②describe 输出不变 ③开关可回退 |
| **M2 小模型** | distill_emotion (3k→15k) → 1.5B 验证 → 3B QLoRA → emotion_serve → 评价层切小模型 | M1 | ①蒸馏 JSON 可解析率>95% ②人工抽检准确率达标 ③推理 2-3s |
| **M3 主动性改造** | 由头候选 + prompt 注入 + want_score 降级 + 无由头不主动 + proactive_outcome 闭环 (需#1) | M1+M2 | 设计文档第十节验收 2/3/4 |
| **M4 深度** | reverie 念头绑定情绪 + event_emotion 训练类别 (数据层) + 主模型可选重训 | M3 | 情绪驱动 eval 提升, 不回归 |

每阶段结束：跑回归 (eval_v2 + 红线/防注入/人格消融)，破坏即回退。

---

## 六、风险清单（会前功尽弃的点 → 对策）

| # | 风险 | 对策 |
|---|---|---|
| 1 | **训练格式 ≠ 推理格式** (接口漂移) | 蒸馏生成器与 emotion_serve 共用同一输入/输出模板文件；QC 校验输出 schema |
| 2 | **EmotionEvent schema 中途改** | `schema_version` 字段 + 落盘前一次性定稿；升级只加字段 |
| 3 | **事件源遗漏** → 情绪史不完整 | 事件源清单冻结在本文档，每个采集点有代码锚点 (第四节) |
| 4 | **教师数据污染** → 小模型学垃圾 | 蒸馏输出走 QC 闸门 (JSON可解析/截断/空值/范围检查)，复用 cleanup 思路 |
| 5 | **主模型 prompt 漂移** (describe 变了) | describe 兼容层 + 每阶段回归 eval |
| 6 | **小模型拖慢聊天** | 聊天事件同步但 2-3s；后台事件入队异步；失败降级不阻塞 |
| 7 | **网页版闭环缺发送通道** | M2 前置: 网页版加"她主动说"推送 (或标注闭环为 QQ 版特性) |
| 8 | **与聊天优先锁冲突** | 情绪采集也尊重 CHAT_ACTIVE (聊天中后台事件排队) |

---

## 七、待拍板（M0 前定）— ✅ 2026-08-27 雾弥全部拍板（按建议）

1. **网页版加"她主动说"推送** ✅ — M2 前置: monitor 面板展示她的主动消息（proactive 闭环在网页版成立）
2. **纯无聊 = 最低优先级由头** ✅ — 可触发，但只能发一句日常问候，**禁止编具体动机**；有事件由头时优先
3. **1.5B 验证先行** ✅ — 半小时验证数据管线再上 3B
4. **情绪记录上限** ✅ — 最近 500 条 + 高 relevance 长留存，低 relevance 超期淘汰
