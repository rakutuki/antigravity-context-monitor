# Context Monitor for Google Antigravity

[English](./README.md) | [日本語](./README.ja.md) | 简体中文

**适用于 Google Antigravity 桌面应用（Antigravity 2.0）右侧边栏的官方规范 MCP App 插件：实时可视化当前会话的“上下文使用量”与“各模型配额用量（Usage）”，并支持一键复制 `/compact` 在合适时机执行会话压缩。**

---

## 💡 为什么需要这个插件？

可以帮助 Antigravity 用户避免长对话中最容易遇到的 **两大痛点**：

1. **防止上下文膨胀后无意识快速消耗配额（Usage）**
   * Antigravity 的使用配额（5小时限额 / 每周限额）按 **每轮 AI 读取的上下文总量**（历史消息、工具输出、文件内容、Skills、Rules 等）计算，而不是按消息条数计算。
   * 当上下文膨胀到 200k–400k tokens 后继续提问，每一次交互都会消耗大量配额。
2. **避免任务中途触发意外的自动压缩（Auto-compaction）**
   * 上下文过大时，系统会在任务中途强制压缩早期对话，有时会导致刚确认的细节或上下文丢失。
   * **关注进度条并在合适的阶段点击“复制 `/compact`”执行压缩**，既能保留关键上下文，又能显著节省配额。

---

## ✨ 主要功能

* **简洁进度条 & 一键/单项展开详情**
  * 默认仅占用极小空间展示使用率 (`%`) 与 `已使用 / 参考上限`（统一为 `k` 表示法）。
  * 点击右上角 **“详情 / 简洁”** 按钮可同时展开或折叠上下文卡片与用量卡片，也可单独点击各个卡片切换展开状态。
  * 展开后可清晰查看 **消息 / 系统工具 / MCP 工具 / 系统提示词 / Skills / 记忆文件** 占比，以及 **剩余空间**、**已缓存上下文**、**对话步数**、**工具调用次数** 与 **产物文件（Artifacts）**。
* **一键“复制 `/compact`” & `/compact` 后即时刷新重置**
  * 可查看距离参考上限的剩余 Token 数（与 **剩余空间** 一致，仅作为参考展示，**不会修改 Antigravity 本体的自动压缩设置**），一键复制 `/compact` 并粘贴到聊天框运行（通过剪贴板可避免 MCP App `ui/message` 误将指令当作普通消息发送）。
  * 通过检测会话日志中即时写入的 `CHECKPOINT` 记录，**无需等待发送下一条消息** 即可在执行 `/compact` 后立刻重置动态 Token 累计与缓存率（同时保留大幅下降兜底检测）。
* **自动跟随模式（`🔄 自动跟随`）与固定会话切换**
  * 在会话下拉框顶部选择 `🔄 自动跟随`，主界面切换聊天或重命名会话时插件也会自动切换追踪当前活跃会话。
  * **※ 关于 Antigravity 本体行为的说明**：新建聊天后 **在发送第 1 条消息前**，磁盘尚未生成会话日志且宿主暂未下发新会话 ID，因此面板会继续显示上一个活跃会话（可在顶部 `最新: <会话名>` 查看）。发送第 1 条消息后将自动切换至新会话。
  * 也可以选择任意历史会话并固定显示，随时查看其上下文构成或产物文件。
* **参考上限・折叠状态・界面语言自动持久化（支持中/英/日 & 统一 `k` 表示法）**
  * 压缩参考上限（`200k` / `400k` / `1,000k` / `2,000k` 等，范围 `10k`–`10,000k`）、卡片折叠状态以及语言设置均以原子写入方式自动保存至插件数据目录下的 `settings.json`。
  * 所有 Token 数值均统一采用直观的 **`k` 表示法**（如 `136k / 400k`、`1,000k`），可在详情面板 **“显示语言 / Language”** 中切换 `Auto`（自动跟随 Antigravity 语言设置）、`日本語`、`English`、`简体中文`。
* **浅色 / 深色主题自动适配**
  * 自动同步 Antigravity 主应用的主题配色与 CSS 变量。
* **分模型用量配额（Usage）环形指示器**
  * 在底部卡片展示 **Gemini Models** / **Claude and GPT models** / **Gemini 4 Argon** 等模型组的 **每周限额（Weekly limit）** 与 **5小时限额（5h limit）** 剩余百分比及重置倒计时。
* **零外部依赖・纯本地安全运行**
  * 仅依赖 Python 标准库（Python 3.9+），采用官方 MCP App（`ui://` + `aux_pane`）规范，无需任何第三方包或对应用本体打补丁。

---

## 🚀 安装方法

### 环境要求
* Google Antigravity 桌面应用（Antigravity 2.0）
* Python 3.9 或更高版本（无需 `pip install`）

### Windows（PowerShell）
运行以下命令克隆至 `~/.gemini/config/plugins/context-monitor`：

```powershell
git clone https://github.com/rakutuki/antigravity-context-monitor.git "$HOME\.gemini\config\plugins\context-monitor"
```
（或下载 ZIP 压缩包并解压至 `C:\Users\<用户名>\.gemini\config\plugins\context-monitor`）

### macOS / Linux
```bash
git clone https://github.com/rakutuki/antigravity-context-monitor.git ~/.gemini/config/plugins/context-monitor
chmod +x ~/.gemini/config/plugins/context-monitor/bin/run_server.sh
```
在 macOS / Linux 上请将 `mcp_config.json` 修改为以下内容：

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

## 📖 使用方法

1. 将目录放置到位后，Antigravity 会自动识别插件（请在左侧边栏 **Skills & Customizations > Plugins** 中确认 **Context Monitor** 已开启）。
2. 点击右侧边栏顶部的 **“`+`”** 按钮，选择 **“`Context`”** 即可打开面板。
3. 也可以直接在聊天框让助手帮你打开（例如：“帮我在右侧打开 Context Monitor”）。

---

## 📁 目录结构

```text
context-monitor/
├── plugin.json                  # 插件清单
├── mcp_config.json              # MCP 服务启动配置
├── README.md                    # 说明文档（英文）
├── README.ja.md                 # 说明文档（日文）
├── README.zh-CN.md              # 说明文档（简体中文）
├── LICENSE                      # MIT License
├── assets/
│   └── logo.svg                 # 插件图标
├── bin/
│   ├── server.py                # MCP 服务器主程序（JSON-RPC 2.0 与工具定义）
│   ├── cm_analyzer.py           # Token 估算与日志解析（LRU 缓存、CHECKPOINT / /compact 即时重置）
│   ├── cm_session.py            # 活跃会话探测、标题/模型识别与环境统计
│   ├── cm_quota.py              # Language Server RPC 用量配额获取
│   ├── cm_settings.py           # 设置持久化（settings.json 原子读写）
│   ├── run_server.cmd           # Windows Python 启动脚本
│   └── run_server.sh            # macOS / Linux 启动脚本
├── tests/
│   └── test_context_monitor.py  # 基于标准库 unittest 的单元测试套件
└── ui/
    └── app.html                 # MCP App 前端界面
```

---

## 🧪 运行单元测试

所有单元测试均基于 Python 标准库 `unittest`，无需真实会话或 Language Server 即可离线运行。

在 `context-monitor` 根目录下执行：

```bash
python -m unittest discover -s tests -v
```

---

## 📜 开源协议
[MIT License](./LICENSE)
