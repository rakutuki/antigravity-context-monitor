#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Monitor MCP Server for Antigravity
現在のセッション（および過去のセッション）の transcript_full.jsonl を解析し、
コンテキスト使用量・最大値バー・カテゴリ別詳細を右枠（Aux Pane）に表示します。
"""

import argparse
import json
import sys
from pathlib import Path

# 同一ディレクトリの分割モジュールを確実にインポート可能にする
BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import cm_analyzer  # noqa: E402
import cm_session   # noqa: E402
import cm_settings  # noqa: E402

APP_URI = "ui://context-monitor/app.html"
PLUGIN_ROOT = BIN_DIR.parent
UI_HTML_PATH = PLUGIN_ROOT / "ui" / "app.html"
DEFAULT_DATA_DIR = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "context-monitor"

# 起動引数 --data_dir で上書きされる永続化ディレクトリ
DATA_DIR = DEFAULT_DATA_DIR

# --- MCP Tool Definitions ---

TOOLS = [
    {
        "name": "open_context_monitor",
        "title": "Context",
        "description": "現在のセッションのコンテキスト使用量（シンプルバーと展開可能なカテゴリ別内訳）を右枠（サイドペイン）に表示します。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "表示するセッションID（省略時は最新のアクティブセッション）"
                },
                "threshold": {
                    "type": "integer",
                    "description": "圧縮の目安上限トークン数（表示上の100%基準・デフォルト: 保存済み設定または 400000）"
                }
            }
        },
        "_meta": {
            "ui": {
                "resourceUri": APP_URI,
                "visibility": ["model", "app"]
            },
            "com.google.antigravity/ui": {
                "entrypoints": [
                    {"type": "aux_pane", "title": "Context"},
                    {"type": "global", "title": "Context"}
                ]
            }
        }
    },
    {
        "name": "refresh_context_stats",
        "title": "コンテキスト統計の更新",
        "description": "UIアプリから最新のコンテキスト使用量を再取得するための専用ツール",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "threshold": {"type": "integer"}
            }
        },
        "_meta": {
            "ui": {
                "visibility": ["app"]
            }
        }
    },
    {
        "name": "peek_active_session",
        "title": "アクティブセッションの高速検知",
        "description": "メイン画面で開いているセッションが切り替わったかどうかだけを軽量・高速に判定します",
        "inputSchema": {
            "type": "object",
            "properties": {}
        },
        "_meta": {
            "ui": {
                "visibility": ["app"]
            }
        }
    },
    {
        "name": "save_context_settings",
        "title": "設定の保存",
        "description": "しきい値やUI折りたたみ状態を永続保存するアプリ専用ツール",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "integer"},
                "expanded": {"type": "boolean"},
                "usageCollapsed": {"type": "boolean"},
                "language": {"type": "string", "enum": ["auto", "ja", "en", "zh"]}
            }
        },
        "_meta": {
            "ui": {
                "visibility": ["app"]
            }
        }
    }
]


def load_ui_html() -> str:
    """UI 用の自己完結 HTML を読み込む"""
    if UI_HTML_PATH.exists():
        return UI_HTML_PATH.read_text(encoding="utf-8")
    return "<html><body><h3>UI HTML file not found</h3></body></html>"


def send_response(msg_id, result):
    """JSON-RPC 2.0 成功レスポンスを送信する"""
    resp = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_error(msg_id, code: int, message: str):
    """JSON-RPC 2.0 エラーレスポンスを送信する"""
    resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _resolve_threshold(raw_threshold, saved_settings: dict) -> int:
    """引数の threshold を解決・クランプし、未指定や不正値時は保存済み設定を返す"""
    saved_val = saved_settings.get("threshold", cm_settings.DEFAULT_THRESHOLD)
    if raw_threshold is None:
        return saved_val
    return cm_settings.clamp_threshold(raw_threshold, default=saved_val)


def handle_request(req: dict, data_dir: Path = None):
    """JSON-RPC 2.0 リクエストを処理する"""
    if not isinstance(req, dict):
        send_error(None, -32600, "Invalid Request")
        return

    method = req.get("method", "")
    msg_id = req.get("id")
    params = req.get("params") or {}
    effective_data_dir = data_dir if data_dir is not None else DATA_DIR

    try:
        if method == "initialize":
            protocol_version = params.get("protocolVersion", "2024-11-05")
            send_response(msg_id, {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False}
                },
                "serverInfo": {
                    "name": "context-monitor",
                    "version": "1.0.0"
                }
            })
        elif isinstance(method, str) and method.startswith("notifications/"):
            # Notification にはレスポンスを返さない
            return
        elif method == "ping":
            send_response(msg_id, {})
        elif method == "tools/list":
            send_response(msg_id, {"tools": TOOLS})
        elif method == "resources/list":
            send_response(msg_id, {
                "resources": [
                    {
                        "uri": APP_URI,
                        "name": "Context Monitor UI",
                        "description": "コンテキスト使用量モニターUI（シンプルバー＆詳細展開）",
                        "mimeType": "text/html;profile=mcp-app",
                        "_meta": {
                            "ui": {
                                "prefersBorder": False
                            }
                        }
                    }
                ]
            })
        elif method == "resources/read":
            uri = params.get("uri", "")
            if uri == APP_URI:
                send_response(msg_id, {
                    "contents": [
                        {
                            "uri": APP_URI,
                            "mimeType": "text/html;profile=mcp-app",
                            "text": load_ui_html(),
                            "_meta": {
                                "ui": {
                                    "prefersBorder": False
                                }
                            }
                        }
                    ]
                })
            else:
                if msg_id is not None:
                    send_error(msg_id, -32602, f"Unknown resource URI: {uri}")
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}

            if name == "peek_active_session":
                active_id = cm_session.discover_currently_viewed_session_id()
                if not active_id:
                    recent = cm_session.list_recent_sessions(limit=1)
                    active_id = recent[0]["id"] if recent else ""
                send_response(msg_id, {
                    "content": [{"type": "text", "text": active_id}],
                    "structuredContent": {"activeSessionId": active_id}
                })
            elif name == "save_context_settings":
                saved = cm_settings.save_settings(effective_data_dir, args)
                send_response(msg_id, {
                    "content": [{"type": "text", "text": "設定を保存しました"}],
                    "structuredContent": {"savedSettings": saved}
                })
            elif name in ("open_context_monitor", "refresh_context_stats"):
                saved_settings = cm_settings.load_settings(effective_data_dir)
                raw_sid = args.get("session_id")
                if isinstance(raw_sid, str):
                    session_id = raw_sid.strip() or None
                elif raw_sid is None:
                    session_id = None
                else:
                    session_id = str(raw_sid).strip() or None
                threshold = _resolve_threshold(args.get("threshold"), saved_settings)

                stats = cm_analyzer.analyze_session(
                    session_id=session_id,
                    auto_compact_threshold=threshold,
                    saved_settings=saved_settings,
                )
                used = stats.get("totalUsedTokens", 0)
                pct = stats.get("usagePercent", 0)
                stitle = stats.get("sessionTitle") or stats.get("sessionId", "")[:8]
                summary_text = (
                    f"セッション「{stitle}」(`{stats.get('sessionId', '')[:8]}`) のコンテキスト使用量: "
                    f"{used:,} / {threshold:,} トークン ({pct}%)"
                )
                send_response(msg_id, {
                    "content": [
                        {"type": "text", "text": summary_text}
                    ],
                    "structuredContent": stats
                })
            else:
                if msg_id is not None:
                    send_error(msg_id, -32601, f"Unknown tool: {name}")
        else:
            if msg_id is not None:
                send_error(msg_id, -32601, f"Method not found: {method}")
    except Exception as e:
        sys.stderr.write(f"Error handling request ({method}): {e}\n")
        sys.stderr.flush()
        if msg_id is not None:
            send_error(msg_id, -32603, f"Internal error: {e}")


def parse_args(argv=None):
    """CLI 引数をパースして --data_dir を取得する"""
    parser = argparse.ArgumentParser(description="Context Monitor MCP Server")
    parser.add_argument(
        "--data_dir",
        type=str,
        default=str(DEFAULT_DATA_DIR),
        help="プラグイン設定・データ永続化ディレクトリ",
    )
    args, _ = parser.parse_known_args(argv)
    return args


def main():
    global DATA_DIR
    args = parse_args()
    if args.data_dir:
        DATA_DIR = Path(args.data_dir)

    for line in sys.stdin:
        line = line.lstrip("\ufeff").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"Parse error: {e}\n")
            sys.stderr.flush()
            send_error(None, -32700, "Parse error")
            continue
        handle_request(req, data_dir=DATA_DIR)


if __name__ == "__main__":
    main()
