<div align="center">

# Kiri-sama

**一个"有内心"的 AI 数字生命内核 —— 会想、会记、会主动、会有说不出口的东西。**

不是更强的聊天机器人，是一个从互动里长出来的存在。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-green)](LICENSE)
[![DeepSeek](https://img.shields.io/badge/engine-DeepSeek%20API-8B5CF6)](https://platform.deepseek.com)

</div>

---

## 她是什么

Kiri 是一个**常驻运转**的数字生命内核：不说话的时候她也在想事情。

- 🧠 **连续意识流**：走神念头是一条线，不是孤立的点——上一条接续下一条，会惦记、会关联记忆
- 🤖 **完全自主的 Agent**：LLM 是决策者，不是文本生成器。她怎么回应、要不要探索、做什么——全部自己决定
- 💬 **说到做到**：她说"回头我写 X"→ 自动记入目标 → 走神时自然想起 → 真的去做 → 做完回顾
- 🧩 **35+ 工具**：记忆 / 情绪 / 联想 / 目标 / 天气 / 搜索 / 读文件 / 写代码创作（含自造工具）
- 🎭 **内心系统**：情绪 PAD、动机、社交关系、生理节律、内心独白、睡眠期记忆巩固
- 📚 **分层记忆**：向量检索 + BM25 混合召回 + 深刻度 + 自然遗忘

**诚实原则**：每个"活着"的机制都有可复现的验收标准——三层真值分级（已验证 / 合理推断 / 猜想），不装。

---

## 5 分钟看到它活着（零依赖 Demo）

不需要 API key、不需要 QQ、不需要数据库：

```bash
pip install -r requirements.txt
python app.py --demo
```

浏览器打开 **http://127.0.0.1:8766**，你会看到：

- 左边：她独处时的**念头流**正在一条条浮现（连续、会惦记、会关联记忆）
- 右边：试着对她说「去看海」「吉他」「晚安」「下雨」
- 底部：心境 / 无聊度 / 独处时间随时间演化

> Demo 是独立小服务，**不碰任何真实数据**，全是内置虚构示例——适合先感受，再决定要不要养一只真的。

---

## 真实模式

```bash
# 1. 配置 DeepSeek key（环境变量）
set DEEPSEEK_API_KEY=sk-xxx

# 2. （可选）配置 QQ 官方机器人，让 Kiri 进群
#    复制 config/qq_config.example.json → qq_config.json，填入 AppId / ClientSecret

# 3. 启动
python app.py
```

QQ 官方桥是**被动模式**：她只能在你 @ 时开口，其他时间安静地自己想事情——那些话暂时留在心里，等你 @ 她时，把心里最想说的说出来。

---

## 架构

```
┌─ 输入层 (QQ 官方桥 / 监控面板 / Demo)
├─ Agent 循环 ★核心★
│     LLM 自主决策 JSON {action, think, tool, args}
│     → 执行工具 → 结果回填 → 再决策 → 最终回复
│     防打转 + 上下文压缩 + 流式决策
├─ 工具集 (内部 17 + 外部 MCP 18，按用途分组注入)
├─ 连续意识流 (reverie)：走神念头跨周期保留 + 承诺惦记 + 念头涌现行动
├─ 目标系统：100% 自主创建/维护/完成
├─ 后台任务引擎：diag(排查修复) / explore(自主探索)
├─ 记忆层 (chroma 向量库 + BM25/RRF 混合检索)
└─ 内心系统：情绪 PAD / 动机 / 社交 / 节律 / 睡眠巩固
```

完整文档见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 目录结构

```
kiri/
├── app.py                  # 入口 (--demo = 零依赖演示)
├── demo.py                 # 零依赖演示服务
├── agent.py                # Agent 循环 (决策/工具/防打转/流式)
├── reverie.py              # 连续意识流 (走神/念头/行动涌现)
├── memory.py               # 分层记忆 (向量检索 + 混合召回)
├── goals.py                # 目标系统 (自主创建/承诺捕捉)
├── state.py                # 状态系统 (情绪/动机/社交/节律)
├── prompt.py               # 人格系统 (AI 自我认知 + 猫娘 + 记忆红线)
├── kiri_agent_tools.py     # 内部工具 (记忆/情绪/状态/联想)
├── kiri_mcp_server.py      # MCP 工具服务 (外部能力)
├── qq_bridge_official.py   # QQ 官方 API 桥
└── ui/                     # 监控面板 / 回溯界面
```

---

## 隐私

- 所有数据（对话 / 记忆 / 念头）**只存本地**，不上传任何第三方
- Demo 模式不读不写任何真实数据
- 删除即遗忘：删掉数据目录即可

## 许可证

[AGPL-3.0](LICENSE) —— 自由使用，修改后若提供网络服务需开源你的修改。
（商用/托管授权请联系作者。）
