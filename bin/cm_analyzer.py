#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Monitor トークン・ログ解析モジュール
transcript_full.jsonl を解析し、カテゴリ別トークン内訳と圧縮タイミングを計算します。
"""

import json
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import cm_quota
import cm_session
import cm_settings

# ログ解析キャッシュ: (log_path, mtime, size) -> 解析済み生データ（最大32エントリ）
_LOG_PARSE_CACHE = OrderedDict()
_LOG_CACHE_MAX_SIZE = 32

_BUILTIN_TOOL_PREFIXES = (
    "read_", "write_", "edit_", "run_", "search_", "update_",
    "ask_", "invoke_", "define_", "manage_", "send_", "list_", "enter_", "exit_",
)


def estimate_tokens_from_text(text: str) -> int:
    """文字数からトークン数を概算（日本語・マルチバイト文字とASCII文字を考慮）"""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    # 英語・コードは約3.8文字で1トークン、日本語・マルチバイトは約1.5文字で1トークン
    return max(1, int(ascii_chars / 3.8 + non_ascii_chars / 1.5))


def _pct(val: int, denom: int) -> float:
    """パーセンテージ（小数点第1位）を安全に算出する"""
    if denom <= 0:
        return 0.0
    return round((val / denom) * 100, 1)


def _is_compaction_drop(prev_total_in: int, current_total_in: int) -> bool:
    """直前ターンから入力トークンが大幅に減少（/compact 発生）したかを判定する"""
    if prev_total_in <= 0 or current_total_in <= 0:
        return False
    drop = prev_total_in - current_total_in
    return drop >= 30000 and current_total_in <= prev_total_in * 0.6


def _parse_log_cached(log_path: Path) -> dict:
    """
    ログファイルを読み込みトークン・ツール情報を集計する。
    (log_path, mtime, size) が同一であればキャッシュを返す。
    """
    stat = log_path.stat()
    cache_key = (str(log_path), stat.st_mtime, stat.st_size)
    if cache_key in _LOG_PARSE_CACHE:
        _LOG_PARSE_CACHE.move_to_end(cache_key)
        return _LOG_PARSE_CACHE[cache_key]

    steps = []
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(json.loads(line))
            except Exception:
                continue

    first_input_tokens = 0
    latest_input_tokens = 0
    latest_cache_tokens = 0
    latest_output_tokens = 0
    total_output_tokens_all_turns = 0
    prev_total_in = 0

    model_name = "gemini"
    selected_model_setting = ""
    last_non_hydrogen_model = ""
    backing_model_hint = ""

    user_tokens = 0
    assistant_tokens = 0
    thinking_tokens = 0
    sys_tool_tokens = 0
    mcp_tool_tokens = 0
    skill_tokens = 0
    memory_tokens = 0

    tool_call_counts = {}
    last_tool_name = ""
    sel_re = re.compile(r"changed setting `Model Selection` from .+? to ([^.\n<]+)")

    for step in steps:
        stype = step.get("type", "")
        content = step.get("content", "") or ""
        thinking = step.get("thinking", "") or ""

        if stype == "USER_INPUT":
            for m_sel in sel_re.finditer(content):
                val = m_sel.group(1).strip()
                if val and val.lower() != "none":
                    selected_model_setting = val
                    backing_model_hint = val
            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.S)
            clean_user = m.group(1) if m else content
            user_tokens += estimate_tokens_from_text(clean_user)

        elif stype == "PLANNER_RESPONSE":
            if step.get("model"):
                step_m = step["model"]
                sm_lower = step_m.lower()
                if any(k in sm_lower for k in ("hydrogen", "orchestrator", "coordinator")):
                    model_name = step_m
                    if selected_model_setting:
                        backing_model_hint = selected_model_setting
                    elif last_non_hydrogen_model:
                        backing_model_hint = last_non_hydrogen_model
                else:
                    model_name = step_m
                    last_non_hydrogen_model = step_m
                    backing_model_hint = step_m
                    selected_model_setting = ""

            inp = int(step.get("input_tokens", 0) or 0)
            cache = int(step.get("cache_read_tokens", 0) or 0)
            out = int(step.get("output_tokens", 0) or 0)
            total_in = inp + cache

            if first_input_tokens == 0 and total_in > 0:
                first_input_tokens = total_in

            # コンパクション（大幅な入力トークン減少）を検知したら動的積算値をリセット
            if _is_compaction_drop(prev_total_in, total_in):
                user_tokens = 0
                assistant_tokens = 0
                thinking_tokens = 0
                sys_tool_tokens = 0
                mcp_tool_tokens = 0
                skill_tokens = 0
                memory_tokens = 0
                total_output_tokens_all_turns = 0

            if total_in > 0:
                latest_input_tokens = inp
                latest_cache_tokens = cache
                latest_output_tokens = out
                prev_total_in = total_in

            total_output_tokens_all_turns += out
            assistant_tokens += estimate_tokens_from_text(content)
            thinking_tokens += estimate_tokens_from_text(thinking)

            tcalls = step.get("tool_calls") or []
            for tc in tcalls:
                tname = tc.get("name", "unknown")
                tool_call_counts[tname] = tool_call_counts.get(tname, 0) + 1
                last_tool_name = tname
                args_str = json.dumps(tc.get("args", {}), ensure_ascii=False)
                args_tok = estimate_tokens_from_text(args_str)
                if tname.startswith("mcp_") or ("_" in tname and not tname.startswith(_BUILTIN_TOOL_PREFIXES)):
                    mcp_tool_tokens += args_tok
                else:
                    sys_tool_tokens += args_tok

        elif stype == "GENERIC":
            tok = estimate_tokens_from_text(content)
            if "builtin\\skills" in content or "builtin/skills" in content or "SKILL.md" in content:
                skill_tokens += tok
            elif "GEMINI.md" in content or "AGENTS.md" in content:
                memory_tokens += tok
            elif last_tool_name.startswith("mcp_"):
                mcp_tool_tokens += tok
            else:
                sys_tool_tokens += tok

        elif stype == "CHECKPOINT":
            status = str(step.get("status") or "").upper()
            summary_tok = int(step.get("output_tokens", 0) or 0)
            if summary_tok <= 0 and content:
                summary_tok = estimate_tokens_from_text(content)
            # 失敗・キャンセルされた CHECKPOINT は無視し、成立した圧縮のみ即時反映する
            if status not in ("ERROR", "FAILED", "CANCELLED") and (status in ("", "DONE") or summary_tok > 0):
                user_tokens = summary_tok
                assistant_tokens = 0
                thinking_tokens = 0
                sys_tool_tokens = 0
                mcp_tool_tokens = 0
                skill_tokens = 0
                memory_tokens = 0
                total_output_tokens_all_turns = 0
                # 次回 PLANNER_RESPONSE まで API 実測値をリセットし、
                # _allocate_category_tokens のフォールバックで (base_system_tokens + summary_tok) を算出させる
                latest_input_tokens = 0
                latest_cache_tokens = 0
                latest_output_tokens = 0
                prev_total_in = 0

    first_user_est = max(50, estimate_tokens_from_text(steps[0].get("content", "")) if steps else 100)

    parsed = {
        "steps": steps,
        "step_count": len(steps),
        "first_input_tokens": first_input_tokens,
        "first_user_est": first_user_est,
        "latest_input_tokens": latest_input_tokens,
        "latest_cache_tokens": latest_cache_tokens,
        "latest_output_tokens": latest_output_tokens,
        "total_output_tokens_all_turns": total_output_tokens_all_turns,
        "model_name": model_name,
        "selected_model_setting": selected_model_setting,
        "backing_model_hint": backing_model_hint,
        "user_tokens": user_tokens,
        "assistant_tokens": assistant_tokens,
        "thinking_tokens": thinking_tokens,
        "sys_tool_tokens": sys_tool_tokens,
        "mcp_tool_tokens": mcp_tool_tokens,
        "skill_tokens": skill_tokens,
        "memory_tokens": memory_tokens,
        "tool_call_counts": tool_call_counts,
    }

    _LOG_PARSE_CACHE[cache_key] = parsed
    while len(_LOG_PARSE_CACHE) > _LOG_CACHE_MAX_SIZE:
        _LOG_PARSE_CACHE.popitem(last=False)

    return parsed


def _allocate_category_tokens(base_system_tokens: int, parsed: dict) -> tuple:
    """
    固定システムトークン（base_system_tokens）と動的会話トークンを分離して集計し、
    reported_total に合わせて動的増分のみをスケーリングする。
    """
    base_sys_tools_schema = int(base_system_tokens * 0.42)
    base_sys_prompt = int(base_system_tokens * 0.34)
    base_skills_catalog = int(base_system_tokens * 0.14)
    base_memory_rules = int(base_system_tokens * 0.07)
    base_mcp_schema = max(
        0,
        base_system_tokens - (base_sys_tools_schema + base_sys_prompt + base_skills_catalog + base_memory_rules),
    )

    fixed = {
        "messages": 0,
        "system_tools": base_sys_tools_schema,
        "mcp_tools": base_mcp_schema,
        "system_prompt": base_sys_prompt,
        "skills": base_skills_catalog,
        "memory_files": base_memory_rules,
    }
    fixed_sum = sum(fixed.values())

    dyn = {
        "messages": parsed["user_tokens"] + max(
            parsed["total_output_tokens_all_turns"],
            parsed["assistant_tokens"] + parsed["thinking_tokens"],
        ),
        "system_tools": parsed["sys_tool_tokens"],
        "mcp_tools": parsed["mcp_tool_tokens"],
        "system_prompt": 0,
        "skills": parsed["skill_tokens"],
        "memory_files": parsed["memory_tokens"],
    }
    dyn_sum = sum(dyn.values())

    reported_total = (
        parsed["latest_input_tokens"]
        + parsed["latest_cache_tokens"]
        + parsed["latest_output_tokens"]
    )

    if reported_total > fixed_sum:
        total_used_tokens = reported_total
        dynamic_budget = reported_total - fixed_sum
        scaled_dyn = {k: 0 for k in dyn}
        if dyn_sum > 0:
            scale = dynamic_budget / dyn_sum
            for k in dyn:
                scaled_dyn[k] = int(round(dyn[k] * scale))
            diff = dynamic_budget - sum(scaled_dyn.values())
            scaled_dyn["messages"] = max(0, scaled_dyn["messages"] + diff)
        else:
            scaled_dyn["messages"] = dynamic_budget
        raw_categories = {k: fixed[k] + scaled_dyn[k] for k in fixed}
    elif reported_total > 0:
        total_used_tokens = reported_total
        combined = {k: fixed[k] + dyn[k] for k in fixed}
        combined_sum = sum(combined.values())
        if combined_sum > 0:
            scale = reported_total / combined_sum
            raw_categories = {k: int(round(combined[k] * scale)) for k in combined}
            diff = reported_total - sum(raw_categories.values())
            max_key = max(raw_categories, key=lambda k: raw_categories[k])
            raw_categories[max_key] = max(0, raw_categories[max_key] + diff)
        else:
            raw_categories = combined
    else:
        raw_categories = {k: fixed[k] + dyn[k] for k in fixed}
        total_used_tokens = sum(raw_categories.values())

    return total_used_tokens, raw_categories


_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def build_empty_session_stats(
    sessions: list = None,
    max_tokens: int = 1000000,
    auto_compact_threshold: int = 400000,
    saved_settings: dict = None,
    session_id: str = "new",
) -> dict:
    """
    セッションログが存在しない場合（新規・未送信セッション時）の初期コンテキスト状態を構築して返す。
    """
    effective_sid = (session_id or "new").strip() or "new"
    base_system_tokens = 14500
    base_sys_tools_schema = int(base_system_tokens * 0.42)
    base_sys_prompt = int(base_system_tokens * 0.34)
    base_skills_catalog = int(base_system_tokens * 0.14)
    base_memory_rules = int(base_system_tokens * 0.07)
    base_mcp_schema = max(
        0,
        base_system_tokens - (base_sys_tools_schema + base_sys_prompt + base_skills_catalog + base_memory_rules),
    )

    denom = auto_compact_threshold if auto_compact_threshold > 0 else max_tokens

    categories = [
        {
            "id": "messages",
            "label": "メッセージ",
            "color": "#3b82f6",
            "tokens": 0,
            "percent": 0.0,
            "detail": "新規セッション（メッセージ未送信）",
        },
        {
            "id": "system_tools",
            "label": "システムツール",
            "color": "#f97316",
            "tokens": base_sys_tools_schema,
            "percent": _pct(base_sys_tools_schema, denom),
            "detail": "組み込みツールスキーマ (初期状態)",
        },
        {
            "id": "mcp_tools",
            "label": "MCP ツール",
            "color": "#10b981",
            "tokens": base_mcp_schema,
            "percent": _pct(base_mcp_schema, denom),
            "detail": "MCP ツール定義",
        },
        {
            "id": "system_prompt",
            "label": "システムプロンプト",
            "color": "#eab308",
            "tokens": base_sys_prompt,
            "percent": _pct(base_sys_prompt, denom),
            "detail": "コア指示・ガイドライン・環境メタデータ",
        },
        {
            "id": "skills",
            "label": "Skills",
            "color": "#94a3b8",
            "tokens": base_skills_catalog,
            "percent": _pct(base_skills_catalog, denom),
            "detail": "スキルカタログ目次",
        },
        {
            "id": "memory_files",
            "label": "メモリファイル / Rules",
            "color": "#64748b",
            "tokens": base_memory_rules,
            "percent": _pct(base_memory_rules, denom),
            "detail": "グローバルルール (GEMINI.md / AGENTS.md)",
        },
    ]

    buffer_tokens = int(auto_compact_threshold * 0.0825)
    remaining_to_threshold = max(0, auto_compact_threshold - base_system_tokens - buffer_tokens)
    free_tokens_in_window = max(0, auto_compact_threshold - base_system_tokens)

    effective_model = (
        (cm_session.detect_session_specific_model(effective_sid) if effective_sid != "new" else "")
        or cm_session.detect_global_default_model()
        or "gemini"
    )
    env_info = cm_session.count_environment_items()
    usage_groups, quota_status = cm_quota.fetch_usage_quotas()
    active_model_usage = cm_quota.select_active_model_usage(effective_model, usage_groups)
    now_str = datetime.now().strftime("%m/%d %H:%M:%S")

    session_list = [
        {"id": s["id"], "title": s["title"], "updatedAt": s["updatedAt"]}
        for s in (sessions or [])[:10]
    ]
    if not any(s["id"] == effective_sid for s in session_list):
        session_list.insert(
            0,
            {"id": effective_sid, "title": "新規セッション (未送信)", "updatedAt": now_str},
        )
        session_list = session_list[:10]

    return {
        "sessionId": effective_sid,
        "sessionTitle": "新規セッション (未送信)",
        "updatedAt": now_str,
        "model": effective_model,
        "displayModel": cm_quota.format_display_model_name(effective_model),
        "stepCount": 0,
        "totalUsedTokens": base_system_tokens,
        "latestInputTokens": base_system_tokens,
        "latestCacheTokens": 0,
        "latestOutputTokens": 0,
        "cacheHitRate": 0.0,
        "autoCompactThreshold": auto_compact_threshold,
        "maxModelTokens": max_tokens,
        "usagePercent": _pct(base_system_tokens, denom),
        "bufferTokens": buffer_tokens,
        "bufferPercent": _pct(buffer_tokens, denom),
        "freeTokens": free_tokens_in_window,
        "freePercent": _pct(free_tokens_in_window, denom),
        "tokensUntilAutoCompact": remaining_to_threshold,
        "categories": categories,
        "toolCallCounts": {},
        "skillsCount": env_info["skillsCount"],
        "artifacts": [],
        "sessions": session_list,
        "usageGroups": usage_groups,
        "activeModelUsage": active_model_usage,
        "quotaStatus": quota_status,
        "savedSettings": saved_settings or dict(cm_settings.DEFAULT_SETTINGS),
    }


def analyze_session(
    session_id: str = None,
    max_tokens: int = 1000000,
    auto_compact_threshold: int = 400000,
    saved_settings: dict = None,
) -> dict:
    """指定されたセッション（未指定なら最新アクティブセッション）のコンテキスト内訳を計算"""
    sessions = cm_session.list_recent_sessions(limit=20)
    effective_session_id = session_id or cm_session.discover_currently_viewed_session_id()
    if not sessions:
        return build_empty_session_stats(
            sessions=[],
            max_tokens=max_tokens,
            auto_compact_threshold=auto_compact_threshold,
            saved_settings=saved_settings,
            session_id=effective_session_id or "new",
        )

    target = None
    if effective_session_id:
        for s in sessions:
            if s["id"] == effective_session_id or s["id"].startswith(effective_session_id):
                target = s
                break
    if not target:
        if effective_session_id and _UUID_PATTERN.match(effective_session_id.strip()):
            return build_empty_session_stats(
                sessions=sessions,
                max_tokens=max_tokens,
                auto_compact_threshold=auto_compact_threshold,
                saved_settings=saved_settings,
                session_id=effective_session_id.strip(),
            )
        target = sessions[0]

    log_path = Path(target["logPath"])
    session_dir = cm_session.BRAIN_DIR / target["id"]

    # アーティファクト一覧の取得
    artifacts = []
    if session_dir.exists():
        for p in sorted(session_dir.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
            if p.is_file() and not p.name.startswith("."):
                artifacts.append({
                    "name": p.name,
                    "uri": p.as_uri(),
                    "sizeBytes": p.stat().st_size,
                })

    parsed = _parse_log_cached(log_path)
    steps = parsed["steps"]

    first_input_tokens = parsed["first_input_tokens"]
    base_system_tokens = (
        max(2000, first_input_tokens - parsed["first_user_est"])
        if first_input_tokens > 0
        else 14500
    )

    total_used_tokens, raw_categories = _allocate_category_tokens(base_system_tokens, parsed)

    buffer_tokens = int(auto_compact_threshold * 0.0825)
    remaining_to_threshold = max(0, auto_compact_threshold - total_used_tokens - buffer_tokens)
    free_tokens_in_window = max(0, auto_compact_threshold - total_used_tokens)
    denom = auto_compact_threshold if auto_compact_threshold > 0 else max_tokens

    tool_call_counts = parsed["tool_call_counts"]

    categories = [
        {
            "id": "messages",
            "label": "メッセージ",
            "color": "#3b82f6",
            "tokens": raw_categories["messages"],
            "percent": _pct(raw_categories["messages"], denom),
            "detail": f"ユーザー入力 + エージェント応答 ({parsed['step_count']} ステップ)",
        },
        {
            "id": "system_tools",
            "label": "システムツール",
            "color": "#f97316",
            "tokens": raw_categories["system_tools"],
            "percent": _pct(raw_categories["system_tools"], denom),
            "detail": f"組み込みツールスキーマ & 実行結果 ({sum(tool_call_counts.values())} 回実行)",
        },
        {
            "id": "mcp_tools",
            "label": "MCP ツール",
            "color": "#10b981",
            "tokens": raw_categories["mcp_tools"],
            "percent": _pct(raw_categories["mcp_tools"], denom),
            "detail": "MCP ツール定義 & レスポンス",
        },
        {
            "id": "system_prompt",
            "label": "システムプロンプト",
            "color": "#eab308",
            "tokens": raw_categories["system_prompt"],
            "percent": _pct(raw_categories["system_prompt"], denom),
            "detail": "コア指示・ガイドライン・環境メタデータ",
        },
        {
            "id": "skills",
            "label": "Skills",
            "color": "#94a3b8",
            "tokens": raw_categories["skills"],
            "percent": _pct(raw_categories["skills"], denom),
            "detail": "スキルカタログ & 読み込まれた SKILL.md",
        },
        {
            "id": "memory_files",
            "label": "メモリファイル / Rules",
            "color": "#64748b",
            "tokens": raw_categories["memory_files"],
            "percent": _pct(raw_categories["memory_files"], denom),
            "detail": "グローバルルール (GEMINI.md / AGENTS.md)",
        },
    ]

    if parsed["selected_model_setting"]:
        effective_model = parsed["selected_model_setting"]
    else:
        effective_model = parsed["model_name"]

    roster_path = session_dir / ".system_generated" / "swarm" / "members.json"
    if roster_path.exists():
        try:
            robj = json.loads(roster_path.read_text(encoding="utf-8", errors="replace"))
            coord_model = ((robj.get("members") or {}).get("coordinator") or {}).get("model")
            if coord_model:
                effective_model = coord_model
        except Exception:
            pass

    session_meta_model = cm_session.detect_session_specific_model(target["id"])
    if session_meta_model:
        effective_model = session_meta_model
    elif effective_model == "gemini":
        g_model = cm_session.detect_global_default_model()
        if g_model:
            effective_model = g_model

    backing_model = ""
    em_lower = effective_model.lower()
    if any(k in em_lower for k in ("hydrogen", "orchestrator", "coordinator")):
        backing_model = (
            parsed.get("backing_model_hint")
            or cm_session.detect_orchestrator_backing_model(session_dir, steps)
            or cm_session.detect_global_default_model()
        )

    env_info = cm_session.count_environment_items()
    usage_groups, quota_status = cm_quota.fetch_usage_quotas()
    active_model_usage = cm_quota.select_active_model_usage(
        effective_model, usage_groups, backing_model=backing_model
    )

    if active_model_usage and active_model_usage.get("displayModelName"):
        display_model = active_model_usage["displayModelName"]
    elif any(k in em_lower for k in ("hydrogen", "orchestrator", "coordinator")) and backing_model:
        display_model = f"Orchestrator ({cm_quota.format_display_model_name(backing_model)})"
    else:
        display_model = cm_quota.format_display_model_name(effective_model)

    latest_in = parsed["latest_input_tokens"]
    latest_cache = parsed["latest_cache_tokens"]

    return {
        "sessionId": target["id"],
        "sessionTitle": target["title"],
        "updatedAt": target["updatedAt"],
        "model": effective_model,
        "displayModel": display_model,
        "stepCount": parsed["step_count"],
        "totalUsedTokens": total_used_tokens,
        "latestInputTokens": latest_in,
        "latestCacheTokens": latest_cache,
        "latestOutputTokens": parsed["latest_output_tokens"],
        "cacheHitRate": _pct(latest_cache, latest_in + latest_cache),
        "autoCompactThreshold": auto_compact_threshold,
        "maxModelTokens": max_tokens,
        "usagePercent": _pct(total_used_tokens, denom),
        "bufferTokens": buffer_tokens,
        "bufferPercent": _pct(buffer_tokens, denom),
        "freeTokens": free_tokens_in_window,
        "freePercent": _pct(free_tokens_in_window, denom),
        "tokensUntilAutoCompact": remaining_to_threshold,
        "categories": categories,
        "toolCallCounts": tool_call_counts,
        "skillsCount": env_info["skillsCount"],
        "artifacts": artifacts[:10],
        "sessions": [
            {"id": s["id"], "title": s["title"], "updatedAt": s["updatedAt"]}
            for s in sessions[:10]
        ],
        "usageGroups": usage_groups,
        "activeModelUsage": active_model_usage,
        "quotaStatus": quota_status,
        "savedSettings": saved_settings or dict(cm_settings.DEFAULT_SETTINGS),
    }
