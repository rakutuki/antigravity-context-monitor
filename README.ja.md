# Context Monitor for Google Antigravity

[English](./README.md) | 日本語 | [简体中文](./README.zh-CN.md)

**Antigravity デスクトップアプリ（Antigravity 2.0）の右枠（サイドペイン）で、現在のセッションの「コンテキスト使用量」と「モデル別使用量（Usage / Quota）」をリアルタイムに可視化し、ベストなタイミングで `/compact` をコピーしてセッション圧縮を行える公式仕様 MCP App プラグインです。**

---

## 💡 なぜこのプラグインが必要なの？

Antigravity を使い始めた方が最もつまずきやすい **2つの罠** を防ぎます。

1. **「コンテキストが膨らんだまま会話して、使用量（Quota）が一瞬で溶ける」のを防ぐ**
   * Antigravity の使用量（5時間上限 / 週間上限）は、メッセージの回数ではなく **「AIが1回のターンで読み込んだコンテキスト（過去の会話・ツール出力・ファイル内容・スキル・ルール）の総量」** に比例して消費されます。
   * コンテキストが 200k〜400k トークンに膨らんだ状態で質問を続けると、1回の質問ごとに大量のクォータが消費されてしまいます。
2. **「突然の自動圧縮（Auto-compaction）で文脈が飛ぶ」のを防ぐ**
   * コンテキストが大きくなりすぎると、作業の途中でも強制的に古い会話が要約（自動圧縮）され、直前の細かい方針や文脈が失われることがあります。
   * **「バーが増えてきたら、作業のキリの良いタイミングで『`/compact` をコピー』ボタンを押してチャット欄から圧縮する」** ことで、文脈を綺麗に保ちながら使用量を大幅に節約できます。

---

## ✨ 主な機能

* **シンプルバー表示 ＆ 一括・個別詳細展開**
  * 普段は「使用率 (`%`)」と「現在値 / 目安上限 (`k` 表記)」だけを省スペースに表示。
  * 最右上の **「詳細 / シンプル」** ボタンでコンテキストカードと使用量カードの両方を一括開閉できるほか、各カードをクリックして個別に開閉することもできます。
  * 展開時は **メッセージ / システムツール / MCPツール / システムプロンプト / Skills / メモリファイル** の内訳に加え、**空き容量**・**キャッシュ済みコンテキスト**・**会話ステップ数**・**ツール呼び出し回数**・**成果物** を確認できます。
* **ワンクリック「`/compact` をコピー」＆ `/compact` 実行時の即時反映**
  * 目安上限までの残りトークン数（表示上の目安であり、**Antigravity 本体の自動圧縮設定は変更されません**）を確認しながら、ワンクリックで `/compact` をクリップボードにコピーしてチャット欄へ貼り付け・実行できます（MCP App の `ui/message` 経由による誤送信を防ぐためクリップボード経由を採用しています）。
  * `/compact` 実行時にログへ書き込まれる `CHECKPOINT` エントリを検知し、**次のメッセージ送信を待たずに圧縮後のコンテキスト量・キャッシュ率へ即時リセット**します（トークン急減フォールバック検知も併用）。
* **自動追従モード（`🔄 自動追従`）と特定セッション固定の切り替え**
  * プルダウンの先頭にある `🔄 自動追従` を選んでおくと、メイン画面で別チャットを開いた際にも自動で追従して最新のセッション名（名前変更後にも対応）と使用量を表示します。
  * **※ Antigravity 本体の仕様に関するご注意**: 新規チャット画面を開いて **1回目のメッセージを送信する前** は、ディスク上にセッションログが生成されずホストからも新規セッションIDが通知されないため、直前のアクティブセッションが表示されます（カード上部の `最新: <セッション名>` で確認できます）。1通目を送信した直後に自動で新規セッションへ切り替わります。
  * 特定の過去セッションを選択すれば、そのセッションに固定して詳細や生成された成果物（Artifacts）を右枠で確認できます。
* **目安上限・折りたたみ設定・表示言語の自動永続化（日英中切替 & `k` 表記統一）**
  * 圧縮の目安上限（`200k` / `400k` / `1,000k` / `2,000k` など、範囲 `10k`〜`10,000k`）・詳細内訳・使用量の開閉・表示言語設定はプラグインデータディレクトリ（`settings.json`）にアトミックに自動保存され、次回開いたときも引き継がれます。
  * トークン数は多言語共通で見やすい **`k` 表記**（例: `136k / 400k`, `1,000k`）に統一されており、詳細パネル内の **「表示言語 / Language」** から `Auto`（Antigravity 本体の言語設定に自動追従）・`日本語`・`English`・`简体中文` を切り替えられます。
* **ライト／ダークテーマ連動**
  * Antigravity アプリ側のホストテーマ設定に応じて、パネルの配色がライトテーマ／ダークテーマへ自動的に切り替わります。
* **モデル別 Usage（クォータ残量）リングゲージ**
  * パネル下部に **Gemini Models** / **Claude and GPT models** の **週間上限（Weekly limit）**・**5時間上限（5h limit）** とリセットまでの残り時間をリングゲージ付きで表示します（最小化時は各グループのミニチップを横並び表示）。
* **完全ローカル・外部パッケージ不要**
  * Python 標準ライブラリのみで動作し、アプリ本体のパッチや改変を一切行わない安全な公式 MCP App（`ui://` + `aux_pane`）方式です。

---

## 🚀 インストール方法

### 前提条件
* Google Antigravity デスクトップアプリ（Antigravity 2.0）
* Python 3.9 以上がインストールされていること（追加の `pip install` は不要です）

### Windows の場合（PowerShell）
以下のコマンドを実行して `~/.gemini/config/plugins/context-monitor` に配置します：

```powershell
git clone https://github.com/rakutuki/antigravity-context-monitor.git "$HOME\.gemini\config\plugins\context-monitor"
```
（または ZIP をダウンロードして `C:\Users\<ユーザー名>\.gemini\config\plugins\context-monitor` に解凍してください）

### macOS / Linux の場合
```bash
git clone https://github.com/rakutuki/antigravity-context-monitor.git ~/.gemini/config/plugins/context-monitor
chmod +x ~/.gemini/config/plugins/context-monitor/bin/run_server.sh
```
macOS / Linux でお使いの場合は、`mcp_config.json` を以下のように変更してください：

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

## 📖 使い方

1. フォルダを配置すると Antigravity が自動的にプラグインを認識します（左サイドバーの **Skills & Customizations > Plugins** で **Context Monitor** が ON になっていることを確認してください）。
2. 画面右側のサイドペイン上部にある **「`+`」ボタン** をクリックし、メニューから **「`Context`」** を選択すると右枠にパネルが表示されます。
3. チャット欄で「コンテキストモニターを開いて」とエージェントに頼んで開かせることもできます。

---

## 📁 ファイル構成

```text
context-monitor/
├── plugin.json                  # プラグインマニフェスト
├── mcp_config.json              # MCP サーバー起動設定
├── README.md                    # ドキュメント（英語）
├── README.ja.md                 # ドキュメント（日本語）
├── README.zh-CN.md              # ドキュメント（簡体字中国語）
├── LICENSE                      # MIT License
├── assets/
│   └── logo.svg                 # プラグインアイコン
├── bin/
│   ├── server.py                # MCP サーバー本体（JSON-RPC 2.0 リクエスト処理・ツール定義）
│   ├── cm_analyzer.py           # トークン概算・ログ解析（LRU キャッシュ・CHECKPOINT / /compact 即時再集計）
│   ├── cm_session.py            # アクティブセッション検出・タイトル/モデル解決・環境集計
│   ├── cm_quota.py              # Language Server RPC 連携による Usage / クォータ取得
│   ├── cm_settings.py           # 目安上限・折りたたみ・言語設定の永続化（settings.json）
│   ├── run_server.cmd           # Windows 用 Python 自動検出ランチャー
│   └── run_server.sh            # macOS / Linux 用ランチャー
├── tests/
│   └── test_context_monitor.py  # 標準ライブラリ unittest による単体テストスイート
└── ui/
    └── app.html                 # MCP App フロントエンド UI（シンプルバー・詳細内訳・Usageゲージ）
```

---

## 🧪 開発者・検証向け：単体テストの実行方法

本プラグインのテストは Python 標準ライブラリの `unittest` のみで構成されており、実環境のセッションデータや Language Server に依存せずローカルで完結して実行できます。

プラグインのルートディレクトリ（`context-monitor`）で以下のコマンドを実行してください：

```bash
python -m unittest discover -s tests -v
```

---

## 📜 ライセンス
[MIT License](./LICENSE)
