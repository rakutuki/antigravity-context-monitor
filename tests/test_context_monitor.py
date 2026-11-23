#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Monitor プラグイン 単体テストスイート
外部環境（実ユーザーの ~/.gemini/antigravity や Language Server）に依存せず、
標準ライブラリの unittest と tempfile / unittest.mock のみで高速・決定的に検証します。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# bin 配下のモジュールをインポートできるようにパスを追加
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PLUGIN_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import cm_analyzer  # noqa: E402
import cm_quota     # noqa: E402
import cm_session   # noqa: E402
import cm_settings  # noqa: E402
import server       # noqa: E402


def _write_jsonl(path: Path, records: list) -> None:
    """テスト用の JSONL ログファイルを書き出すヘルパー"""
    lines = [json.dumps(rec, ensure_ascii=False) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestTokenEstimationAndCompaction(unittest.TestCase):
    """トークン概算・固定システム枠・コンパクション検知・ログキャッシュのテスト"""

    def setUp(self):
        """各テスト実行前にモジュール内キャッシュを初期化する"""
        cm_analyzer._LOG_PARSE_CACHE.clear()
        cm_quota._QUOTA_CACHE = {"timestamp": 0, "data": None, "status": "unavailable"}
        cm_session._SUBAGENT_IDS_CACHE = {"timestamp": 0, "ids": set()}
        cm_session._TITLE_CACHE.clear()
        cm_session._FALLBACK_TITLE_CACHE.clear()
        cm_session._SESSION_MODEL_CACHE.clear()

    def test_estimate_tokens_from_text_density(self):
        """日本語テキストと ASCII テキストでトークン密度が異なり、空文字が 0 を返すこと"""
        self.assertEqual(cm_analyzer.estimate_tokens_from_text(""), 0)

        ascii_text = "a" * 60
        ja_text = "あ" * 60

        ascii_tokens = cm_analyzer.estimate_tokens_from_text(ascii_text)
        ja_tokens = cm_analyzer.estimate_tokens_from_text(ja_text)

        self.assertGreater(ascii_tokens, 0)
        self.assertGreater(ja_tokens, ascii_tokens)
        self.assertGreater(ja_tokens / len(ja_text), ascii_tokens / len(ascii_text))

    def test_fixed_system_tokens_remain_constant_on_conversation_growth(self):
        """会話が増えても固定システムトークンが伸縮せず、合計が reported_total と一致すること"""
        base_system_tokens = 14500
        expected_sys_prompt = int(base_system_tokens * 0.34)

        parsed_short = {
            "user_tokens": 200,
            "assistant_tokens": 400,
            "thinking_tokens": 100,
            "sys_tool_tokens": 300,
            "mcp_tool_tokens": 50,
            "skill_tokens": 0,
            "memory_tokens": 0,
            "total_output_tokens_all_turns": 500,
            "latest_input_tokens": 18000,
            "latest_cache_tokens": 5000,
            "latest_output_tokens": 500,
        }
        total_short, cats_short = cm_analyzer._allocate_category_tokens(base_system_tokens, parsed_short)
        reported_short = 18000 + 5000 + 500
        self.assertEqual(total_short, reported_short)
        self.assertEqual(sum(cats_short.values()), reported_short)
        self.assertEqual(cats_short["system_prompt"], expected_sys_prompt)

        # 会話が増分した状態でも system_prompt の値は固定維持される
        parsed_long = dict(parsed_short)
        parsed_long.update({
            "user_tokens": 5000,
            "assistant_tokens": 8000,
            "total_output_tokens_all_turns": 9000,
            "latest_input_tokens": 60000,
            "latest_cache_tokens": 20000,
            "latest_output_tokens": 1200,
        })
        total_long, cats_long = cm_analyzer._allocate_category_tokens(base_system_tokens, parsed_long)
        reported_long = 60000 + 20000 + 1200
        self.assertEqual(total_long, reported_long)
        self.assertEqual(sum(cats_long.values()), reported_long)
        self.assertEqual(cats_long["system_prompt"], expected_sys_prompt)
        self.assertEqual(cats_long["system_prompt"], cats_short["system_prompt"])

    def test_analyze_session_keeps_system_prompt_fixed_across_turns(self):
        """analyze_session 経由でも会話増分に対して system_prompt が固定維持されること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            brain_dir = Path(tmpdir) / "brain"
            sess_a = brain_dir / "11111111-1111-1111-1111-111111111111"
            sess_b = brain_dir / "22222222-2222-2222-2222-222222222222"
            log_a = sess_a / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_b = sess_b / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_a.parent.mkdir(parents=True, exist_ok=True)
            log_b.parent.mkdir(parents=True, exist_ok=True)

            first_user = {"type": "USER_INPUT", "content": "<USER_REQUEST>最初の質問です</USER_REQUEST>"}
            first_planner = {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-neon",
                "content": "最初の応答です。",
                "input_tokens": 12000,
                "cache_read_tokens": 4000,
                "output_tokens": 400,
            }
            _write_jsonl(log_a, [first_user, first_planner])
            _write_jsonl(
                log_b,
                [
                    first_user,
                    first_planner,
                    {"type": "USER_INPUT", "content": "<USER_REQUEST>" + ("追加の質問です。" * 20) + "</USER_REQUEST>"},
                    {
                        "type": "PLANNER_RESPONSE",
                        "model": "gemini-4-neon",
                        "content": "詳細な回答です。" * 30,
                        "input_tokens": 45000,
                        "cache_read_tokens": 20000,
                        "output_tokens": 1500,
                    },
                ],
            )

            fake_sessions = [
                {
                    "id": sess_a.name,
                    "title": "短いセッション",
                    "updatedAt": "01/01 10:00:00",
                    "logPath": str(log_a),
                },
                {
                    "id": sess_b.name,
                    "title": "長いセッション",
                    "updatedAt": "01/01 10:05:00",
                    "logPath": str(log_b),
                },
            ]

            with mock.patch.object(cm_session, "BRAIN_DIR", brain_dir), \
                 mock.patch.object(cm_session, "list_recent_sessions", return_value=fake_sessions), \
                 mock.patch.object(cm_session, "discover_currently_viewed_session_id", return_value=""), \
                 mock.patch.object(cm_session, "detect_session_specific_model", return_value=""), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 3}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                res_a = cm_analyzer.analyze_session(session_id=sess_a.name)
                res_b = cm_analyzer.analyze_session(session_id=sess_b.name)

            cats_a = {c["id"]: c["tokens"] for c in res_a["categories"]}
            cats_b = {c["id"]: c["tokens"] for c in res_b["categories"]}

            self.assertEqual(cats_a["system_prompt"], cats_b["system_prompt"])
            self.assertEqual(res_a["totalUsedTokens"], 12000 + 4000 + 400)
            self.assertEqual(sum(cats_a.values()), res_a["totalUsedTokens"])
            self.assertEqual(res_b["totalUsedTokens"], 45000 + 20000 + 1500)
            self.assertEqual(sum(cats_b.values()), res_b["totalUsedTokens"])

    def test_compaction_drop_resets_dynamic_token_accumulation(self):
        """入力トークンが 40%以上かつ 30,000以上減少したとき動的トークン積算がリセットされること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            brain_dir = Path(tmpdir) / "brain"
            sess_dir = brain_dir / "33333333-3333-3333-3333-333333333333"
            log_path = sess_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # ターン1（圧縮前）に大量のツール・スキル・メモリ読み込みが発生し、
            # ターン2（/compact 発生後）に入力トークンが 100,000 -> 20,000 へ急減するシナリオ
            records = [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>" + ("長い指示" * 100) + "</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "圧縮前の長い応答" * 50,
                    "thinking": "圧縮前の思考" * 50,
                    "input_tokens": 80000,
                    "cache_read_tokens": 20000,
                    "output_tokens": 2500,
                    "tool_calls": [
                        {"name": "mcp_github_get_issue", "args": {"repo": "example", "id": 1}},
                    ],
                },
                {"type": "GENERIC", "content": "C:\\path\\builtin\\skills\\sample\\SKILL.md を読み込みました"},
                {"type": "GENERIC", "content": "GEMINI.md ルールを適用しました"},
                {"type": "USER_INPUT", "content": "<USER_REQUEST>圧縮直後のプロンプト</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "圧縮しました",
                    "thinking": "確認",
                    "input_tokens": 15000,
                    "cache_read_tokens": 5000,
                    "output_tokens": 300,
                },
                {"type": "USER_INPUT", "content": "<USER_REQUEST>続けて</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "承知しました",
                    "thinking": "処理",
                    "input_tokens": 15500,
                    "cache_read_tokens": 5000,
                    "output_tokens": 200,
                },
            ]
            _write_jsonl(log_path, records)

            parsed = cm_analyzer._parse_log_cached(log_path)

            # 圧縮前の動的トークン（スキル・メモリ・MCPツール・旧出力合計）はリセットされている
            self.assertEqual(parsed["skill_tokens"], 0)
            self.assertEqual(parsed["memory_tokens"], 0)
            self.assertEqual(parsed["mcp_tool_tokens"], 0)
            self.assertEqual(parsed["total_output_tokens_all_turns"], 300 + 200)
            self.assertEqual(parsed["user_tokens"], cm_analyzer.estimate_tokens_from_text("続けて"))
            self.assertEqual(
                parsed["assistant_tokens"],
                cm_analyzer.estimate_tokens_from_text("圧縮しました")
                + cm_analyzer.estimate_tokens_from_text("承知しました"),
            )
            self.assertEqual(
                parsed["thinking_tokens"],
                cm_analyzer.estimate_tokens_from_text("確認")
                + cm_analyzer.estimate_tokens_from_text("処理"),
            )
            self.assertEqual(parsed["latest_input_tokens"], 15500)
            self.assertEqual(parsed["latest_cache_tokens"], 5000)
            self.assertEqual(parsed["latest_output_tokens"], 200)

            # analyze_session 全体でも圧縮後の合計値が返ること
            fake_sessions = [{
                "id": sess_dir.name,
                "title": "圧縮テスト",
                "updatedAt": "01/01 12:00:00",
                "logPath": str(log_path),
            }]
            with mock.patch.object(cm_session, "BRAIN_DIR", brain_dir), \
                 mock.patch.object(cm_session, "list_recent_sessions", return_value=fake_sessions), \
                 mock.patch.object(cm_session, "discover_currently_viewed_session_id", return_value=""), \
                 mock.patch.object(cm_session, "detect_session_specific_model", return_value=""), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 1}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                stats = cm_analyzer.analyze_session(session_id=sess_dir.name)

            self.assertEqual(stats["totalUsedTokens"], 15500 + 5000 + 200)
            cats = {c["id"]: c["tokens"] for c in stats["categories"]}
            self.assertEqual(sum(cats.values()), stats["totalUsedTokens"])

    def test_parse_log_cached_hits_and_invalidates_correctly(self):
        """同一 mtime・size ではキャッシュヒットして再オープンせず、クリアや更新時には再パースすること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "transcript_full.jsonl"
            _write_jsonl(log_path, [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>テスト1</USER_REQUEST>"},
                {"type": "PLANNER_RESPONSE", "content": "応答1", "input_tokens": 5000, "output_tokens": 100},
            ])

            # 1回目の呼び出しでキャッシュに格納
            first_result = cm_analyzer._parse_log_cached(log_path)
            self.assertEqual(first_result["step_count"], 2)

            # 2回目はキャッシュヒットし Path.open が呼ばれないこと
            with mock.patch.object(Path, "open", autospec=True, side_effect=Path.open) as spy_open:
                second_result = cm_analyzer._parse_log_cached(log_path)
                spy_open.assert_not_called()
            self.assertIs(second_result, first_result)

            # キャッシュクリア後は再度ファイルを開くこと
            cm_analyzer._LOG_PARSE_CACHE.clear()
            with mock.patch.object(Path, "open", autospec=True, side_effect=Path.open) as spy_open:
                third_result = cm_analyzer._parse_log_cached(log_path)
                spy_open.assert_called_once()
            self.assertEqual(third_result["step_count"], 2)

            # ファイル内容を更新（サイズと mtime を変更）したときは再パースされること
            _write_jsonl(log_path, [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>テスト1</USER_REQUEST>"},
                {"type": "PLANNER_RESPONSE", "content": "応答1", "input_tokens": 5000, "output_tokens": 100},
                {"type": "USER_INPUT", "content": "<USER_REQUEST>テスト2</USER_REQUEST>"},
            ])
            st = log_path.stat()
            os.utime(log_path, (st.st_atime + 5, st.st_mtime + 5))

            with mock.patch.object(Path, "open", autospec=True, side_effect=Path.open) as spy_open:
                fourth_result = cm_analyzer._parse_log_cached(log_path)
                spy_open.assert_called_once()
            self.assertEqual(fourth_result["step_count"], 3)

    def test_build_empty_session_stats(self):
        """build_empty_session_stats が NameError を起こさず有効な初期辞書を返すこと"""
        with mock.patch.object(cm_session, "detect_global_default_model", return_value="gemini-4-neon"), \
             mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 5}), \
             mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([{"groupName": "Gemini Models", "items": []}], "ok")):

            res = cm_analyzer.build_empty_session_stats()

        self.assertIsInstance(res, dict)
        self.assertEqual(res["sessionId"], "new")
        self.assertIn("categories", res)
        self.assertIsInstance(res["categories"], list)
        self.assertEqual(len(res["categories"]), 6)
        self.assertIn("savedSettings", res)
        self.assertEqual(res["savedSettings"], cm_settings.DEFAULT_SETTINGS)
        self.assertIn("quotaStatus", res)
        self.assertEqual(res["quotaStatus"], "ok")

    def test_analyze_session_returns_empty_stats_for_unsent_new_session_uuid(self):
        """未送信の新規セッション UUID が渡されたとき直前セッションにフォールバックせず初期状態を返すこと"""
        new_uuid = "44444444-4444-4444-4444-444444444444"
        fake_sessions = [{
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "直前の既存セッション",
            "updatedAt": "01/01 10:00:00",
            "logPath": "/nonexistent/transcript_full.jsonl",
        }]

        with mock.patch.object(cm_session, "list_recent_sessions", return_value=fake_sessions), \
             mock.patch.object(cm_session, "detect_session_specific_model", return_value=""), \
             mock.patch.object(cm_session, "detect_global_default_model", return_value="gemini-4-neon"), \
             mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 4}), \
             mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

            # session_id を明示指定した場合
            res_explicit = cm_analyzer.analyze_session(session_id=new_uuid)
            # 自動検出が新規 UUID を返した場合
            with mock.patch.object(cm_session, "discover_currently_viewed_session_id", return_value=new_uuid):
                res_auto = cm_analyzer.analyze_session(session_id=None)

        for res in (res_explicit, res_auto):
            self.assertEqual(res["sessionId"], new_uuid)
            self.assertEqual(res["stepCount"], 0)
            self.assertEqual(res["sessionTitle"], "新規セッション (未送信)")
            self.assertEqual(res["sessions"][0]["id"], new_uuid)
            self.assertEqual(res["sessions"][1]["id"], fake_sessions[0]["id"])

    def test_checkpoint_immediately_resets_context_before_next_response(self):
        """/compact 直後の CHECKPOINT で次応答を待たずにコンテキストがリセットされ、次応答でシームレスに復帰すること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            brain_dir = Path(tmpdir) / "brain"
            sess_dir = brain_dir / "55555555-5555-5555-5555-555555555555"
            log_path = sess_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # 圧縮前のターン群（初回ターン + 大量トークン蓄積ターン）と /compact 直後の CHECKPOINT
            summary_text = "# Resuming from a compaction\nこれまでの作業要約です。"
            before_records = [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>最初のタスクです</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "開始します。",
                    "input_tokens": 14600,
                    "cache_read_tokens": 0,
                    "output_tokens": 200,
                },
                {"type": "USER_INPUT", "content": "<USER_REQUEST>詳細な分析を実行してください</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "圧縮前の詳細な応答" * 40,
                    "thinking": "圧縮前の長い思考" * 40,
                    "input_tokens": 20000,
                    "cache_read_tokens": 450000,
                    "output_tokens": 700,
                    "tool_calls": [
                        {"name": "mcp_github_get_issue", "args": {"repo": "example", "id": 99}},
                    ],
                },
                {"type": "GENERIC", "content": "C:\\path\\builtin\\skills\\sample\\SKILL.md を読み込みました"},
                {"type": "GENERIC", "content": "GEMINI.md ルールを適用しました"},
                {"type": "USER_INPUT", "content": "<USER_REQUEST>/compact</USER_REQUEST>"},
                {
                    "type": "CHECKPOINT",
                    "status": "DONE",
                    "input_tokens": 500000,
                    "cache_read_tokens": 0,
                    "output_tokens": 4400,
                    "content": summary_text,
                },
            ]
            _write_jsonl(log_path, before_records)

            parsed_after_compact = cm_analyzer._parse_log_cached(log_path)
            self.assertEqual(parsed_after_compact["latest_input_tokens"], 0)
            self.assertEqual(parsed_after_compact["latest_cache_tokens"], 0)
            self.assertEqual(parsed_after_compact["latest_output_tokens"], 0)
            self.assertEqual(parsed_after_compact["user_tokens"], 4400)
            self.assertEqual(parsed_after_compact["assistant_tokens"], 0)
            self.assertEqual(parsed_after_compact["thinking_tokens"], 0)
            self.assertEqual(parsed_after_compact["sys_tool_tokens"], 0)
            self.assertEqual(parsed_after_compact["mcp_tool_tokens"], 0)
            self.assertEqual(parsed_after_compact["skill_tokens"], 0)
            self.assertEqual(parsed_after_compact["memory_tokens"], 0)
            self.assertEqual(parsed_after_compact["total_output_tokens_all_turns"], 0)

            fake_sessions = [{
                "id": sess_dir.name,
                "title": "即時圧縮テスト",
                "updatedAt": "01/01 12:30:00",
                "logPath": str(log_path),
            }]
            with mock.patch.object(cm_session, "BRAIN_DIR", brain_dir), \
                 mock.patch.object(cm_session, "list_recent_sessions", return_value=fake_sessions), \
                 mock.patch.object(cm_session, "discover_currently_viewed_session_id", return_value=""), \
                 mock.patch.object(cm_session, "detect_session_specific_model", return_value=""), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 2}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                stats_after_compact = cm_analyzer.analyze_session(session_id=sess_dir.name)

            # 圧縮前の 470,700 より大幅に減少し、(base_system_tokens + 4400) 相当・キャッシュ0%になること
            base_sys = max(2000, parsed_after_compact["first_input_tokens"] - parsed_after_compact["first_user_est"])
            self.assertLess(stats_after_compact["totalUsedTokens"], 30000)
            self.assertEqual(stats_after_compact["totalUsedTokens"], base_sys + 4400)
            cats_compact = {c["id"]: c["tokens"] for c in stats_after_compact["categories"]}
            self.assertEqual(sum(cats_compact.values()), stats_after_compact["totalUsedTokens"])
            self.assertEqual(stats_after_compact["cacheHitRate"], 0.0)

            # 次のユーザー入力と応答を追記し、mtime を進めて再解析
            next_prompt = "圧縮後の新しい質問です"
            after_records = before_records + [
                {"type": "USER_INPUT", "content": f"<USER_REQUEST>{next_prompt}</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "圧縮後の応答です。",
                    "input_tokens": 23500,
                    "cache_read_tokens": 0,
                    "output_tokens": 500,
                },
            ]
            _write_jsonl(log_path, after_records)
            st = log_path.stat()
            os.utime(log_path, (st.st_atime + 5, st.st_mtime + 5))

            parsed_next = cm_analyzer._parse_log_cached(log_path)
            # CHECKPOINT で prev_total_in=0 となっているため、二重リセットされず加算されること
            expected_user_tokens = 4400 + cm_analyzer.estimate_tokens_from_text(next_prompt)
            self.assertEqual(parsed_next["user_tokens"], expected_user_tokens)

            with mock.patch.object(cm_session, "BRAIN_DIR", brain_dir), \
                 mock.patch.object(cm_session, "list_recent_sessions", return_value=fake_sessions), \
                 mock.patch.object(cm_session, "discover_currently_viewed_session_id", return_value=""), \
                 mock.patch.object(cm_session, "detect_session_specific_model", return_value=""), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 2}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                stats_next = cm_analyzer.analyze_session(session_id=sess_dir.name)

            self.assertEqual(stats_next["totalUsedTokens"], 23500 + 500)
            cats_next = {c["id"]: c["tokens"] for c in stats_next["categories"]}
            self.assertEqual(sum(cats_next.values()), 24000)

    def test_checkpoint_fallback_estimation_and_failed_status_ignored(self):
        """CHECKPOINT の output_tokens 欠損時は本文概算を使用し、失敗ステータスは無視されること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) output_tokens が 0 / 欠損の場合は content から概算すること
            log_fallback = Path(tmpdir) / "fallback.jsonl"
            summary_content = "要約本文のフォールバックテスト用テキストです。" * 10
            _write_jsonl(log_fallback, [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>質問</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "長い応答" * 20,
                    "input_tokens": 50000,
                    "cache_read_tokens": 10000,
                    "output_tokens": 1000,
                },
                {
                    "type": "CHECKPOINT",
                    "status": "DONE",
                    "input_tokens": 60000,
                    "output_tokens": 0,
                    "content": summary_content,
                },
            ])

            parsed_fb = cm_analyzer._parse_log_cached(log_fallback)
            self.assertEqual(parsed_fb["user_tokens"], cm_analyzer.estimate_tokens_from_text(summary_content))
            self.assertEqual(parsed_fb["latest_input_tokens"], 0)
            self.assertEqual(parsed_fb["latest_cache_tokens"], 0)

            # 2) status が ERROR / FAILED の CHECKPOINT は無視され直前状態が維持されること
            log_failed = Path(tmpdir) / "failed.jsonl"
            _write_jsonl(log_failed, [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>テスト質問</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon",
                    "content": "応答テキスト",
                    "input_tokens": 42000,
                    "cache_read_tokens": 8000,
                    "output_tokens": 600,
                },
                {
                    "type": "CHECKPOINT",
                    "status": "ERROR",
                    "input_tokens": 50000,
                    "output_tokens": 100,
                    "content": "失敗した要約",
                },
                {
                    "type": "CHECKPOINT",
                    "status": "FAILED",
                    "input_tokens": 50000,
                    "output_tokens": 100,
                    "content": "同じく失敗した要約",
                },
            ])

            parsed_failed = cm_analyzer._parse_log_cached(log_failed)
            self.assertEqual(parsed_failed["latest_input_tokens"], 42000)
            self.assertEqual(parsed_failed["latest_cache_tokens"], 8000)
            self.assertEqual(parsed_failed["latest_output_tokens"], 600)
            self.assertEqual(parsed_failed["user_tokens"], cm_analyzer.estimate_tokens_from_text("テスト質問"))
            self.assertEqual(parsed_failed["assistant_tokens"], cm_analyzer.estimate_tokens_from_text("応答テキスト"))
            self.assertEqual(parsed_failed["total_output_tokens_all_turns"], 600)


class TestSettingsModule(unittest.TestCase):
    """cm_settings の設定読み書き・クランプ・バリデーションのテスト"""

    def test_clamp_threshold_boundaries_and_invalid_values(self):
        """しきい値が範囲内に丸められ、不正値時はデフォルトへフォールバックすること"""
        self.assertEqual(cm_settings.clamp_threshold(500), cm_settings.MIN_THRESHOLD)
        self.assertEqual(cm_settings.clamp_threshold(-100), cm_settings.MIN_THRESHOLD)
        self.assertEqual(cm_settings.clamp_threshold(99999999), cm_settings.MAX_THRESHOLD)
        self.assertEqual(cm_settings.clamp_threshold(250000), 250000)

        # 文字列や bool、None はデフォルト値にフォールバック
        self.assertEqual(cm_settings.clamp_threshold("invalid"), cm_settings.DEFAULT_THRESHOLD)
        self.assertEqual(cm_settings.clamp_threshold(True), cm_settings.DEFAULT_THRESHOLD)
        self.assertEqual(cm_settings.clamp_threshold(False), cm_settings.DEFAULT_THRESHOLD)
        self.assertEqual(cm_settings.clamp_threshold(None), cm_settings.DEFAULT_THRESHOLD)

    def test_save_and_load_settings_with_filtering(self):
        """一時ディレクトリへの保存・読み込みが機能し、未許可キーが保存されないこと"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            # 初期状態（ファイル未存在）はデフォルトを返す
            initial = cm_settings.load_settings(data_dir)
            self.assertEqual(initial, cm_settings.DEFAULT_SETTINGS)

            # 許可されたキーと不正値・未許可キーを混在させて保存
            saved = cm_settings.save_settings(data_dir, {
                "threshold": 5000,         # MIN_THRESHOLD (10000) にクランプされる
                "expanded": True,
                "usageCollapsed": False,
                "language": "EN",
                "forbiddenKey": "should_not_be_saved",
                "anotherExtra": 12345,
            })
            self.assertEqual(saved["threshold"], 10000)
            self.assertIs(saved["expanded"], True)
            self.assertIs(saved["usageCollapsed"], False)
            self.assertEqual(saved["language"], "en")
            self.assertNotIn("forbiddenKey", saved)
            self.assertNotIn("anotherExtra", saved)

            # ファイルの中身を直接確認し、未許可キーが書き込まれていないことを検証
            raw_json = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(set(raw_json.keys()), {"threshold", "expanded", "usageCollapsed", "language"})

            # 再読み込みしても同じ値が返ること
            reloaded = cm_settings.load_settings(data_dir)
            self.assertEqual(reloaded, saved)

            # threshold / expanded / language に不正値を渡しても既存値が壊れないこと
            updated = cm_settings.save_settings(data_dir, {
                "threshold": "not_a_number",
                "expanded": "not_a_bool",
                "language": "fr",
            })
            self.assertEqual(updated["threshold"], 10000)
            self.assertIs(updated["expanded"], True)
            self.assertEqual(updated["language"], "en")

            # 大文字混在 ("ZH") も小文字 ("zh") に正規化されて保存されること
            res_zh_upper = cm_settings.save_settings(data_dir, {"language": "ZH"})
            self.assertEqual(res_zh_upper["language"], "zh")
            self.assertEqual(cm_settings.load_settings(data_dir)["language"], "zh")

            # 有効な言語コード ("ja", "zh", "auto") への切り替えが保存・復元されること
            for valid_lang in ("ja", "zh", "auto"):
                res_lang = cm_settings.save_settings(data_dir, {"language": valid_lang})
                self.assertEqual(res_lang["language"], valid_lang)
                self.assertEqual(cm_settings.load_settings(data_dir)["language"], valid_lang)


class TestSessionAndQuotaHelpers(unittest.TestCase):
    """cm_session._quote_ident と cm_quota._is_loopback_host の単体テスト"""

    def test_quote_ident_escapes_double_quotes(self):
        """SQLite 識別子のダブルクォートを二重化して安全にエスケープすること"""
        self.assertEqual(cm_session._quote_ident("simple_table"), '"simple_table"')
        self.assertEqual(cm_session._quote_ident('my"table'), '"my""table"')
        self.assertEqual(cm_session._quote_ident('a"b"c'), '"a""b""c"')

    def test_is_loopback_host(self):
        """ループバックホストのみ True を返し、外部ホストには False を返すこと"""
        self.assertTrue(cm_quota._is_loopback_host("https://127.0.0.1:5387/api"))
        self.assertTrue(cm_quota._is_loopback_host("http://localhost:8080/test"))
        self.assertTrue(cm_quota._is_loopback_host("https://[::1]:5387/test"))

        self.assertFalse(cm_quota._is_loopback_host("https://example.com/api"))
        self.assertFalse(cm_quota._is_loopback_host("https://192.168.1.10:5387/api"))
        self.assertFalse(cm_quota._is_loopback_host("http://127.0.0.1.attacker.example/"))
        self.assertFalse(cm_quota._is_loopback_host("not-a-valid-url"))

    def test_discover_currently_viewed_session_id_detects_new_session_in_conversations(self):
        """conversations/ 配下のログ未作成の新規セッション UUID を検出し、サブエージェントは除外すること"""
        old_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        new_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        sub_uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"

        with tempfile.TemporaryDirectory() as tmpdir:
            ag_dir = Path(tmpdir)
            brain_dir = ag_dir / "brain"
            conv_dir = ag_dir / "conversations"
            old_log = brain_dir / old_uuid / ".system_generated" / "logs" / "transcript_full.jsonl"
            old_log.parent.mkdir(parents=True, exist_ok=True)
            conv_dir.mkdir(parents=True, exist_ok=True)

            _write_jsonl(old_log, [{"type": "USER_INPUT", "content": "old"}])
            old_conv = conv_dir / f"{old_uuid}.pb"
            old_conv.write_bytes(b"old")
            os.utime(old_conv, (1000, 1000))

            # ログ未作成の新規セッションファイルを作成（より新しいタイムスタンプ）
            new_conv = conv_dir / f"{new_uuid}.pb"
            new_conv.write_bytes(b"new")
            os.utime(new_conv, (2000, 2000))

            # サブエージェント用のファイルがさらに新しくても除外されること
            sub_conv = conv_dir / f"{sub_uuid}.pb"
            sub_conv.write_bytes(b"sub")
            os.utime(sub_conv, (3000, 3000))

            with mock.patch.object(cm_session, "BRAIN_DIR", brain_dir), \
                 mock.patch.object(cm_session, "get_subagent_ids", return_value={sub_uuid}):
                detected = cm_session.discover_currently_viewed_session_id()

        self.assertEqual(detected, new_uuid)

    def test_model_selection_regex_extracts_cleanly_without_greedy_overflow(self):
        """Model Selection 正規表現が後続の 'No need to comment...' 内の 'to' まで貪欲マッチしないこと"""
        sample_content = (
            "<USER_REQUEST>\nテスト\n</USER_REQUEST>\n"
            "<USER_SETTINGS_CHANGE>\n"
            "The user changed setting `Model Selection` from None to Gemini 4 Neon (Medium). "
            "No need to comment on this change if the user doesn't ask about it.\n"
            "</USER_SETTINGS_CHANGE>"
        )
        steps = [
            {"type": "USER_INPUT", "content": sample_content},
            {"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-xhigh", "content": "計画中"},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            sess_dir = Path(tmpdir) / "55555555-5555-5555-5555-555555555555"
            log_path = sess_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl(log_path, steps)

            bm = cm_session.detect_orchestrator_backing_model(sess_dir, steps)
            cm_analyzer._LOG_PARSE_CACHE.clear()
            parsed = cm_analyzer._parse_log_cached(log_path)

        self.assertEqual(bm, "Gemini 4 Neon (Medium)")
        self.assertEqual(parsed["selected_model_setting"], "Gemini 4 Neon (Medium)")
        self.assertEqual(parsed["backing_model_hint"], "Gemini 4 Neon (Medium)")

    def test_orchestrator_transition_uses_neon_quota_and_never_falls_back_to_argon(self):
        """Neon から hydrogen (Orchestrator) へ遷移したセッションで Argon 枠に誤フォールバックしないこと"""
        usage_groups = [
            {
                "groupName": "Gemini Models",
                "inlineSingle": False,
                "items": [{"windowLabel": "5h limit", "percentLeft": 95, "resetIn": "4h 10m"}],
            },
            {
                "groupName": "Gemini 4 Argon",
                "inlineSingle": True,
                "items": [{"windowLabel": "Weekly limit", "percentLeft": 48, "resetIn": "3d 2h"}],
            },
        ]

        # backing_model 不明の Orchestrator でも Argon 枠の残量が低いだけで Argon にしない
        unknown_usage = cm_quota.select_active_model_usage(
            "gemini-4-hydrogen-xhigh", usage_groups, backing_model=""
        )
        self.assertEqual(unknown_usage["groupName"], "Gemini Models")
        self.assertEqual(unknown_usage["displayModelName"], "Orchestrator")

        # Neon -> hydrogen の遷移ログから analyze_session が Orchestrator (Gemini 4 Neon) を返すこと
        with tempfile.TemporaryDirectory() as tmpdir:
            brain_dir = Path(tmpdir) / "brain"
            sess_dir = brain_dir / "66666666-6666-6666-6666-666666666666"
            log_path = sess_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl(log_path, [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>開始</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon-medium",
                    "content": "応答",
                    "input_tokens": 10000,
                    "output_tokens": 200,
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-hydrogen-xhigh",
                    "content": "計画",
                    "input_tokens": 12000,
                    "output_tokens": 300,
                },
            ])
            fake_sessions = [{
                "id": sess_dir.name,
                "title": "遷移テスト",
                "updatedAt": "01/01 12:00:00",
                "logPath": str(log_path),
            }]
            cm_analyzer._LOG_PARSE_CACHE.clear()
            with mock.patch.object(cm_session, "BRAIN_DIR", brain_dir), \
                 mock.patch.object(cm_session, "list_recent_sessions", return_value=fake_sessions), \
                 mock.patch.object(cm_session, "discover_currently_viewed_session_id", return_value=""), \
                 mock.patch.object(cm_session, "detect_session_specific_model", return_value=""), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 2}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=(usage_groups, "ok")):
                stats = cm_analyzer.analyze_session(session_id=sess_dir.name)

        self.assertEqual(stats["activeModelUsage"]["groupName"], "Gemini Models")
        self.assertEqual(stats["activeModelUsage"]["displayModelName"], "Orchestrator (Gemini 4 Neon)")
        self.assertEqual(stats["displayModel"], "Orchestrator (Gemini 4 Neon)")

    def test_fetch_usage_quotas_uses_shorter_ttl_when_unavailable(self):
        """fetch_usage_quotas の unavailable キャッシュ有効期限が ok 時より短い (2秒) こと"""
        cm_quota._QUOTA_CACHE = {"timestamp": 1000.0, "data": [], "status": "unavailable"}

        # 2秒未満 (1001.5) はキャッシュヒットして RPC を呼ばない
        class _FakeDt:
            now_ts = 1001.5

            @classmethod
            def now(cls):
                class _Obj:
                    @staticmethod
                    def timestamp():
                        return cls.now_ts
                return _Obj()

        with mock.patch.object(cm_quota, "datetime", _FakeDt), \
             mock.patch.object(cm_quota, "call_ls_rpc", return_value=None) as spy_rpc:
            groups, status = cm_quota.fetch_usage_quotas()
            self.assertEqual(status, "unavailable")
            spy_rpc.assert_not_called()

            # 2秒経過 (1002.5) すると unavailable キャッシュが切れて RPC を再試行し、ok へ復帰できる
            _FakeDt.now_ts = 1002.5
            spy_rpc.return_value = {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [{"window": "5h", "remainingFraction": 0.9, "resetTime": ""}],
                    }
                ]
            }
            groups2, status2 = cm_quota.fetch_usage_quotas()
            self.assertEqual(status2, "ok")
            self.assertEqual(len(groups2), 1)
            spy_rpc.assert_called()

            # ok キャッシュは 5秒経過 (1007.5) しても 10秒 TTL 内のため再呼び出ししない
            spy_rpc.reset_mock()
            _FakeDt.now_ts = 1007.5
            groups3, status3 = cm_quota.fetch_usage_quotas()
            self.assertEqual(status3, "ok")
            spy_rpc.assert_not_called()


class TestServerJsonRpc(unittest.TestCase):
    """server.py の JSON-RPC 処理（main / handle_request）のテスト"""

    def _run_handle_request(self, req: dict, data_dir: Path) -> dict:
        """handle_request の stdout 出力をキャプチャして JSON 辞書として返すヘルパー"""
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            server.handle_request(req, data_dir=data_dir)
        out = buf.getvalue().strip()
        return json.loads(out) if out else {}

    def test_main_returns_parse_error_on_invalid_json(self):
        """不正な JSON 文字列を入力したとき -32700 Parse error が返ること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_stdin = io.StringIO("{invalid json string\n")
            fake_stdout = io.StringIO()
            fake_stderr = io.StringIO()

            with mock.patch.object(sys, "stdin", fake_stdin), \
                 mock.patch.object(sys, "stdout", fake_stdout), \
                 mock.patch.object(sys, "stderr", fake_stderr), \
                 mock.patch.object(sys, "argv", ["server.py", "--data_dir", tmpdir]):
                server.main()

            resp = json.loads(fake_stdout.getvalue().strip())
            self.assertEqual(resp["jsonrpc"], "2.0")
            self.assertIsNone(resp["id"])
            self.assertEqual(resp["error"]["code"], -32700)
            self.assertIn("Parse error", resp["error"]["message"])

    def test_unknown_tool_returns_method_not_found(self):
        """未知のツール名で tools/call したとき -32601 が返ること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            resp = self._run_handle_request({
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
            }, Path(tmpdir))

        self.assertEqual(resp["id"], 10)
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertIn("Unknown tool", resp["error"]["message"])

    def test_invalid_threshold_does_not_crash_and_returns_valid_response(self):
        """open_context_monitor / refresh_context_stats に不正な threshold を渡しても有効なレスポンスが返ること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            with mock.patch.object(cm_session, "list_recent_sessions", return_value=[]), \
                 mock.patch.object(cm_session, "detect_global_default_model", return_value="gemini-4-neon"), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 2}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                # 文字列 threshold -> デフォルト値 (400000) にフォールバック
                resp_str = self._run_handle_request({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "open_context_monitor",
                        "arguments": {"threshold": "invalid"},
                    },
                }, data_dir)

                self.assertIn("result", resp_str)
                self.assertEqual(
                    resp_str["result"]["structuredContent"]["autoCompactThreshold"],
                    cm_settings.DEFAULT_THRESHOLD,
                )

                # 負数 threshold -> 最小値 (10000) にクランプ
                resp_neg = self._run_handle_request({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "refresh_context_stats",
                        "arguments": {"threshold": -500},
                    },
                }, data_dir)

                self.assertIn("result", resp_neg)
                self.assertEqual(
                    resp_neg["result"]["structuredContent"]["autoCompactThreshold"],
                    cm_settings.MIN_THRESHOLD,
                )

    def test_save_context_settings_persists_to_next_refresh_context_stats(self):
        """save_context_settings で保存した設定が次の refresh_context_stats に反映されること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            save_resp = self._run_handle_request({
                "jsonrpc": "2.0",
                "id": 101,
                "method": "tools/call",
                "params": {
                    "name": "save_context_settings",
                    "arguments": {
                        "threshold": 250000,
                        "expanded": True,
                        "usageCollapsed": False,
                        "language": "en",
                    },
                },
            }, data_dir)

            self.assertIn("result", save_resp)
            self.assertEqual(save_resp["result"]["structuredContent"]["savedSettings"], {
                "threshold": 250000,
                "expanded": True,
                "usageCollapsed": False,
                "language": "en",
            })

            with mock.patch.object(cm_session, "list_recent_sessions", return_value=[]), \
                 mock.patch.object(cm_session, "detect_global_default_model", return_value="gemini-4-neon"), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 2}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                refresh_resp = self._run_handle_request({
                    "jsonrpc": "2.0",
                    "id": 102,
                    "method": "tools/call",
                    "params": {
                        "name": "refresh_context_stats",
                        "arguments": {},
                    },
                }, data_dir)

            self.assertIn("result", refresh_resp)
            sc = refresh_resp["result"]["structuredContent"]
            self.assertEqual(sc["autoCompactThreshold"], 250000)
            self.assertEqual(sc["savedSettings"], {
                "threshold": 250000,
                "expanded": True,
                "usageCollapsed": False,
                "language": "en",
            })

            # 中国語 ("zh") も save_context_settings 経由で受け入れ・保存・復元されること
            save_zh_resp = self._run_handle_request({
                "jsonrpc": "2.0",
                "id": 103,
                "method": "tools/call",
                "params": {
                    "name": "save_context_settings",
                    "arguments": {"language": "zh"},
                },
            }, data_dir)
            self.assertEqual(save_zh_resp["result"]["structuredContent"]["savedSettings"]["language"], "zh")

            with mock.patch.object(cm_session, "list_recent_sessions", return_value=[]), \
                 mock.patch.object(cm_session, "detect_global_default_model", return_value="gemini-4-neon"), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 2}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                refresh_zh_resp = self._run_handle_request({
                    "jsonrpc": "2.0",
                    "id": 104,
                    "method": "tools/call",
                    "params": {
                        "name": "refresh_context_stats",
                        "arguments": {},
                    },
                }, data_dir)
            self.assertEqual(refresh_zh_resp["result"]["structuredContent"]["savedSettings"]["language"], "zh")

    def test_refresh_context_stats_accepts_omitted_and_blank_session_id(self):
        """refresh_context_stats に session_id 省略や空文字・空白を渡しても正常に最新セッションを解析すること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            brain_dir = Path(tmpdir) / "brain"
            sess_dir = brain_dir / "77777777-7777-7777-7777-777777777777"
            log_path = sess_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl(log_path, [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>テスト</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-neon-high",
                    "content": "応答",
                    "input_tokens": 20000,
                    "output_tokens": 500,
                },
            ])
            fake_sessions = [{
                "id": sess_dir.name,
                "title": "最新セッション",
                "updatedAt": "01/01 12:00:00",
                "logPath": str(log_path),
            }]
            cm_analyzer._LOG_PARSE_CACHE.clear()

            with mock.patch.object(cm_session, "BRAIN_DIR", brain_dir), \
                 mock.patch.object(cm_session, "list_recent_sessions", return_value=fake_sessions), \
                 mock.patch.object(cm_session, "discover_currently_viewed_session_id", return_value=""), \
                 mock.patch.object(cm_session, "detect_session_specific_model", return_value=""), \
                 mock.patch.object(cm_session, "count_environment_items", return_value={"skillsCount": 1}), \
                 mock.patch.object(cm_quota, "fetch_usage_quotas", return_value=([], "unavailable")):

                for idx, args in enumerate([
                    {"threshold": 400000},
                    {"session_id": "", "threshold": 400000},
                    {"session_id": "   ", "threshold": 400000},
                ], start=1):
                    resp = self._run_handle_request({
                        "jsonrpc": "2.0",
                        "id": 200 + idx,
                        "method": "tools/call",
                        "params": {"name": "refresh_context_stats", "arguments": args},
                    }, data_dir)
                    self.assertIn("result", resp)
                    sc = resp["result"]["structuredContent"]
                    self.assertEqual(sc["sessionId"], sess_dir.name)
                    self.assertEqual(sc["totalUsedTokens"], 20500)

    def test_open_context_monitor_entrypoints_use_context_title(self):
        """open_context_monitor の title と entrypoints タイトルが英語・日本語共通の 'Context' であること"""
        tool = next(t for t in server.TOOLS if t["name"] == "open_context_monitor")
        self.assertEqual(tool["title"], "Context")
        entrypoints = tool["_meta"]["com.google.antigravity/ui"]["entrypoints"]
        self.assertEqual(len(entrypoints), 2)
        for ep in entrypoints:
            self.assertEqual(ep["title"], "Context")


if __name__ == "__main__":
    unittest.main()
