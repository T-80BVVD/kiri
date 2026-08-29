<div align="center">

# Kiri-sama

**A persistently-running digital-life kernel — even when you're not talking to her, she keeps thinking on her own.**

Her thoughts form one continuous line. She remembers things she promised to do — and can write a tool herself when she needs one.

**English | [简体中文](README.md)**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-green)](LICENSE)
[![DeepSeek](https://img.shields.io/badge/engine-DeepSeek%20API-8B5CF6)](https://platform.deepseek.com)

</div>

---

## What she is

Kiri is a **persistently-running** digital-life kernel: even when nobody is talking to her, she is still thinking.

- 🧠 **Continuous stream of consciousness** — her wandering thoughts form one line, not isolated dots. Each thought continues from the previous one; she *remembers obligations* and *associates memories*.
- 🤖 **Autonomous agent loop** — the LLM is the *decision-maker*, not a text generator. How she replies, whether to explore, what to do — driven by her own decisions.
- 💬 **She does what she says** — "I'll write X later" → auto-captured as a goal → naturally recalled while wandering → done → reflected upon.
- 🧩 **35+ tools** — memory / emotion / association / goals / weather / search / read files / creation.
- 🛠️ **Can build a tool when she needs one** — she can write one of her own and immediately run it to verify, rather than waiting for a feature to be added.
- 🎭 **Inner system** — PAD emotion, motivation, social relations, biorhythm, inner monologue, sleep-phase memory consolidation.
- 📚 **Layered memory** — vector retrieval + BM25 hybrid recall + salience + natural forgetting.

**Honesty principle** — every "alive" mechanism has a reproducible acceptance test, graded across three levels of truth (verified / reasonable inference / conjecture). What's real and what's still a hypothesis is marked in the code and docs.

---

## See her alive in 5 minutes (zero-dependency Demo)

No API key, no QQ, no database:

```bash
python app.py --demo
```

Open **http://127.0.0.1:8766** in your browser and watch:

- **Left**: her wandering **thought stream** surfacing one after another (continuous, remembering obligations, associating memories)
- **Right**: try saying 「去看海」「吉他」「晚安」「下雨」 (or: "going to the sea" / "guitar" / "good night" / "rain")
- **Bottom**: mood / boredom / alone-time evolving over time

> The demo is a standalone mini-service. It **touches no real data** — all built-in fictional examples. Perfect to feel her out before deciding to raise a real one.

> 💡 The Demo uses **Python standard library only** — no `pip install` needed.

---

## Real mode

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure DeepSeek key (environment variable)
set DEEPSEEK_API_KEY=sk-xxx

# 3. (Optional) Configure a QQ official bot so Kiri can join a group
#    Copy config/qq_config.example.json -> qq_config.json, fill in AppId / ClientSecret

# 4. Start
python app.py
```

The QQ official bridge runs in **passive mode**: she can only speak when you @ her. The rest of the time she quietly thinks to herself — those words stay with her until you @ her, and then she says what she most wanted to.

---

## Architecture

```
┌─ Input layer (QQ official bridge / monitor panel / Demo)
├─ Agent loop ★core★
│     LLM decides via JSON {action, think, tool, args}
│     → execute tool → feed result back → decide again → final reply
│     anti-loop detection + context compaction + streaming decision
├─ Tool set (internal 17 + external MCP 18, injected by purpose group)
├─ Continuous consciousness (reverie): cross-cycle thought + obligation-mind + thought→action
├─ Goal system: 100% self-created / maintained / completed
├─ Background task engine: diag (troubleshoot) / explore (autonomous)
├─ Memory layer (chroma vector DB + BM25/RRF hybrid retrieval)
└─ Inner system: PAD emotion / motivation / social / biorhythm / sleep consolidation
```

Full docs: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Repository layout

```
kiri-public/
├── app.py                     # entry (python app.py = real mode; --demo = zero-dep demo)
├── src/kiri/                  # core source (digital-life kernel)
│   ├── agent.py               # Agent loop (decide/tool/anti-loop/streaming)
│   ├── kiri.py                # main class (multi-user + persona routing)
│   ├── reverie.py             # continuous consciousness (wander/thought/action-emergence)
│   ├── memory.py              # layered memory (vector + BM25/RRF hybrid recall)
│   ├── state.py               # state system (emotion/motivation/social/biorhythm)
│   ├── goals.py               # goal system (self-create/commit-capture)
│   ├── prompt.py              # persona system (AI self-awareness + cat-girl + memory redline)
│   ├── engine.py              # LLM engine wrapper (DeepSeek API + local)
│   ├── kiri_agent_tools.py    # internal tools (memory/emotion/state/association)
│   ├── kiri_mcp_server.py     # MCP tool service (external capabilities)
│   ├── qq_bridge_official.py  # QQ official API bridge
│   ├── monitor_server.py      # background monitor panel
│   ├── demo.py                # zero-dependency demo service
│   ├── emotion/               # emotion core (PAD 4-dim state machine + motivation/social/biorhythm)
│   ├── tests/                 # unit tests (agent loop / memory / QQ bridge)
│   ├── ui/                    # monitor / trace frontend (HTML/JS)
│   └── my_creations/          # example area for Kiri's self-written tools
├── requirements.txt           # real-mode dependencies (demo needs none)
└── ARCHITECTURE.md            # full architecture doc
```

---

## Privacy

- All data (conversations / memories / thoughts) is **stored locally only** — never uploaded to any third party.
- Demo mode reads and writes **no real data**.
- Delete = forget: just remove the data directory.

## License

[AGPL-3.0](LICENSE) — free to use; if you modify it and provide a network service, you must open-source your changes.
(Commercial / hosted licensing: contact the author.)

---

## Roadmap / contributing

This project started as a personal experiment in building a "living" AI. Issues and discussions are welcome. If you want to help — or just want to raise one of your own — see [ARCHITECTURE.md](ARCHITECTURE.md) for the design philosophy and honesty principles.
