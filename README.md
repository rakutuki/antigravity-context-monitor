# Context Monitor for Google Antigravity

English | [日本語](./README.ja.md) | [简体中文](./README.zh-CN.md)

**An official-spec MCP App plugin for the Google Antigravity desktop app (Antigravity 2.0) side pane that monitors your current session's context usage and per-model Usage quotas in real time, helping you compact cleanly at the right moment with one-click `/compact` copy.**

---

## 💡 Why Use Context Monitor?

Antigravity users commonly run into two costly pitfalls when working in long sessions:

1. **Burning through Usage quotas silently with bloated context**
   * Antigravity Usage quotas (5h limit / Weekly limit) scale with the **total context size the AI reads on each turn** (conversation history, tool outputs, file contents, skills, rules), not just the number of messages sent.
   * Continuing a chat at 200k–400k tokens consumes far more quota on every single question.
2. **Unexpected auto-compaction mid-task**
   * When context grows large enough, Antigravity automatically compacts older turns into a summary—sometimes right in the middle of an implementation step.
   * **Keeping an eye on the bar and clicking "Copy `/compact`" at a clean breakpoint** preserves important context while keeping token consumption low.

---

## ✨ Features

* **Compact Bar & One-Click Detailed Breakdown**
  * Shows a clean summary bar (`usage %` and `used / target` in `k` format) when collapsed.
  * Click the **Details / Compact** button at the top-right to expand or collapse both the Context card and the Usage card together, or click each card individually.
  * Breaks usage down into **Messages**, **System Tools**, **MCP Tools**, **System Prompt**, **Skills**, and **Memory Files / Rules**, along with **Free space**, **Cached context**, **Conversation steps**, **Tool calls**, and **Artifacts**.
* **One-Click `Copy /compact` & Immediate Post-Compaction Reset**
  * Shows remaining tokens until your target limit (`-- tokens until target`, matching **Free space**) and copies `/compact` to the clipboard for pasting into the chat input (clipboard copy prevents MCP App `ui/message` from sending `/compact` as plain chat text).
  * Automatically detects `/compact` immediately via `CHECKPOINT` entries in the session log—resetting accumulated dynamic tokens and cached context without waiting for the next user message—and also keeps a large token-drop fallback detector.
* **Auto-Follow Mode (`🔄 Auto-follow`) & Session Pinning**
  * Selecting `🔄 Auto-follow` at the top of the session dropdown automatically tracks the active chat in the main pane (including updated titles).
  * **Note on Antigravity behavior**: Before sending the first message in a brand-new chat window, Antigravity has not written a session log on disk or sent a new conversation ID to the plugin yet, so the previous active session remains shown (clearly labeled as `Latest: <session title>`). As soon as the first message is sent, it switches automatically.
  * You can also pin any recent session from the dropdown to inspect its token breakdown and generated artifacts.
* **Persistent Settings, Unified `k` Notation & Multi-Language UI**
  * Target context limit (`200k` / `400k` / `1,000k` / `2,000k`, clamped within `10k`–`10,000k` — **a reference 100% scale and `/compact` guide; it does not modify Antigravity's internal auto-compact threshold**), card expand/collapse states, and display language (`Auto`, `日本語`, `English`, `简体中文`) are saved atomically to `settings.json` in the plugin data directory.
  * All token counts use clean **`k` notation** (e.g., `136k / 400k`, `1,000k`).
* **Automatic Light / Dark Theme Sync**
  * Follows Antigravity's host theme and CSS variables automatically.
* **Model Usage Quota Ring Gauges**
  * Displays **Weekly limit** / **5h limit** remaining percentages and reset countdowns across model groups (**Gemini Models**, **Claude and GPT models**, **Gemini 4 Argon**) via local Language Server RPC.
* **Zero External Dependencies**
  * Pure Python standard library (`python >= 3.9`) using the official MCP App (`ui://` + `aux_pane`) specification—no app binary patching required.

---

## 🚀 Installation

### Prerequisites
* Google Antigravity desktop app (Antigravity 2.0)
* Python 3.9+ (no `pip install` required)

### Windows (PowerShell)
Clone into `~/.gemini/config/plugins/context-monitor`:

```powershell
git clone https://github.com/rakutuki/antigravity-context-monitor.git "$HOME\.gemini\config\plugins\context-monitor"
```
(Or download the ZIP and extract it to `C:\Users\<username>\.gemini\config\plugins\context-monitor`.)

### macOS / Linux
```bash
git clone https://github.com/rakutuki/antigravity-context-monitor.git ~/.gemini/config/plugins/context-monitor
chmod +x ~/.gemini/config/plugins/context-monitor/bin/run_server.sh
```
On macOS / Linux, update `mcp_config.json` as follows:

```json
{
  "mcpServers": {
    "monitor": {
      "command": "${PLUGIN_ROOT}/bin/run_server.sh",
      "args": [
        "--data_dir=${PLUGIN_DATA}"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

---

## 📖 Usage

1. Once placed in the plugins folder, Antigravity automatically detects **Context Monitor** (verify it is enabled under **Skills & Customizations > Plugins**).
2. Click the **`+`** button at the top of the right-hand side pane and choose **`Context`**.
3. You can also ask the assistant in chat (e.g., *"Open Context Monitor in the side pane"*).

---

## 📁 Project Structure

```text
context-monitor/
├── plugin.json                  # Plugin manifest
├── mcp_config.json              # MCP server launch config
├── README.md                    # Documentation (English)
├── README.ja.md                 # Documentation (Japanese)
├── README.zh-CN.md              # Documentation (Simplified Chinese)
├── LICENSE                      # MIT License
├── assets/
│   └── logo.svg                 # Plugin icon
├── bin/
│   ├── server.py                # MCP server (JSON-RPC 2.0 handlers & tool schemas)
│   ├── cm_analyzer.py           # Token estimation & log analyzer (/compact & CHECKPOINT reset, LRU cache)
│   ├── cm_session.py            # Active session discovery, title/model resolution & env counts
│   ├── cm_quota.py              # Language Server RPC client for Usage quotas
│   ├── cm_settings.py           # Atomic settings persistence (settings.json)
│   ├── run_server.cmd           # Windows Python launcher
│   └── run_server.sh            # macOS / Linux launcher
├── tests/
│   └── test_context_monitor.py  # Standard-library unittest suite
└── ui/
    └── app.html                 # Self-contained MCP App frontend UI
```

---

## 🧪 Running Unit Tests

All unit tests run completely offline using Python's built-in `unittest` without requiring live Antigravity sessions or a running Language Server.

From the `context-monitor` root directory:

```bash
python -m unittest discover -s tests -v
```

---

## 📜 License
[MIT License](./LICENSE)
