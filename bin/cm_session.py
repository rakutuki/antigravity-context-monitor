#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Monitor セッション・メタデータ解決モジュール
アクティブセッション検出、タイトル取得、モデル特定、環境アイテム集計を行います。
"""

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
CONFIG_DIR = Path.home() / ".gemini" / "config"
BUILTIN_SKILLS_DIR = Path.home() / ".gemini" / "antigravity" / "builtin" / "skills"
SUMMARIES_DB_PATH = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
AGENTAPI_BAT = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi.bat"
LS_EXE = (
    Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    / "Programs"
    / "antigravity"
    / "resources"
    / "bin"
    / "language_server.exe"
)

_TITLE_CACHE = {}           # conv_id -> (timestamp, title)
_DB_TITLES_CACHE = {"mtime": 0, "titles": {}}
_SUBAGENT_IDS_CACHE = {"timestamp": 0, "ids": set()}
_FALLBACK_TITLE_CACHE = {}  # conv_id -> (mtime, fallback_title)
_SESSION_MODEL_CACHE = {}   # conv_id -> (timestamp, model)


def _quote_ident(name: str) -> str:
    """SQLite のテーブル名・カラム名などの識別子を二重引用符で安全にエスケープする"""
    return '"' + str(name).replace('"', '""') + '"'


def _build_agentapi_cmd(conv_id: str) -> list:
    """agentapi get-conversation-metadata の実行コマンドを組み立てる"""
    exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
    if exe and Path(exe).exists():
        return [exe, "agentapi", "get-conversation-metadata", conv_id]
    if LS_EXE.exists():
        return [str(LS_EXE), "agentapi", "get-conversation-metadata", conv_id]
    if AGENTAPI_BAT.exists():
        return [str(AGENTAPI_BAT), "agentapi", "get-conversation-metadata", conv_id]
    return ["agentapi", "get-conversation-metadata", conv_id]


def _run_agentapi_metadata(conv_id: str) -> dict:
    """agentapi を実行して会話メタデータ辞書を返す（失敗時は空辞書）"""
    cmd = _build_agentapi_cmd(conv_id)
    try:
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=0.8,
            startupinfo=startupinfo,
        )
        if proc.returncode == 0 and proc.stdout:
            return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        pass
    return {}


def _extract_title_from_metadata(md: dict) -> str:
    """メタデータ辞書からタイトル文字列を抽出する"""
    if not isinstance(md, dict):
        return ""
    return (
        md.get("title")
        or md.get("summary")
        or md.get("name")
        or (md.get("metadata") or {}).get("title")
        or (md.get("conversation") or {}).get("title")
        or ""
    ).strip()


def discover_currently_viewed_session_id() -> str:
    """
    Antigravity デスクトップアプリで現在ユーザーが開いている（フォーカスしている）セッションIDを特定する。
    ログ未作成の新規セッション（1回目のプロンプト送信前）も検出できるよう、
    presence/・conversations/ の UUID 候補と antigravity_state.pbtxt を突合して判定する。
    """
    antigravity_dir = BRAIN_DIR.parent
    uuid_re = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
    subagent_ids = get_subagent_ids()

    def has_log_files(cid: str) -> bool:
        if not cid:
            return False
        d = BRAIN_DIR / cid
        return (
            (d / ".system_generated" / "logs" / "transcript_full.jsonl").exists()
            or (d / ".system_generated" / "logs" / "transcript.jsonl").exists()
        )

    candidates = []  # (mtime, cid)
    discovered_ids = set()

    # 1. presence ディレクトリ（ログ未作成の新規セッションも候補に含める）
    presence_dir = antigravity_dir / "presence"
    try:
        if presence_dir.exists():
            for pf in presence_dir.iterdir():
                if not pf.is_file():
                    continue
                st = pf.stat().st_mtime
                txt = pf.read_text(encoding="utf-8", errors="replace")
                for cid in reversed(uuid_re.findall(txt)):
                    if cid not in subagent_ids:
                        candidates.append((st, cid))
                        discovered_ids.add(cid)
                        break
                m_name = uuid_re.search(pf.name)
                if m_name:
                    cid = m_name.group(1)
                    if cid not in subagent_ids:
                        candidates.append((st, cid))
                        discovered_ids.add(cid)
    except Exception:
        pass

    # 2. conversations ディレクトリ（新規セッション作成時に .pb 等が先に作られる場合も検出）
    conv_dir = antigravity_dir / "conversations"
    try:
        if conv_dir.exists():
            for f in conv_dir.iterdir():
                m = uuid_re.search(f.name)
                if m:
                    cid = m.group(1)
                    if cid not in subagent_ids:
                        candidates.append((f.stat().st_mtime, cid))
                        discovered_ids.add(cid)
    except Exception:
        pass

    # 3. annotations ディレクトリ（ログが存在する有効セッションのみ対象）
    ann_dir = antigravity_dir / "annotations"
    try:
        if ann_dir.exists():
            for f in ann_dir.iterdir():
                m = uuid_re.search(f.name)
                if m:
                    cid = m.group(1)
                    if cid not in subagent_ids and has_log_files(cid):
                        candidates.append((f.stat().st_mtime, cid))
                        discovered_ids.add(cid)
    except Exception:
        pass

    # 4. antigravity_state.pbtxt から最後に記録された有効な会話IDを抽出
    state_pbtxt = antigravity_dir / "antigravity_state.pbtxt"
    try:
        if state_pbtxt.exists():
            st = state_pbtxt.stat().st_mtime
            txt = state_pbtxt.read_text(encoding="utf-8", errors="replace")
            matches = uuid_re.findall(txt)
            for cid in reversed(matches):
                if cid in subagent_ids:
                    continue
                if has_log_files(cid) or (BRAIN_DIR / cid).exists() or cid in discovered_ids:
                    candidates.append((st + 2.0, cid))
                    break
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return ""


def detect_orchestrator_backing_model(session_dir: Path, steps: list) -> str:
    """
    Orchestrator（gemini-4-hydrogen-xhigh 等）のセッションにおいて、
    裏で設定されているメインモデル（Argon / Neon 等）を逆探知する。
    """
    last_setting = ""
    last_non_hydrogen_model = ""
    last_flow_model = ""
    spawned_subagents = []
    uuid_re = re.compile(r'Created the following subagent:\s*\{\s*"conversationId":\s*"([0-9a-f\-]{36})"', re.I)
    sel_re = re.compile(r"changed setting `Model Selection` from .+? to ([^.\n<]+)")

    for st in steps:
        content = st.get("content", "") or ""
        stype = st.get("type")
        if stype == "USER_INPUT":
            for m_sel in sel_re.finditer(content):
                val = m_sel.group(1).strip()
                if val and val.lower() != "none":
                    last_setting = val
                    last_flow_model = val
        elif stype == "PLANNER_RESPONSE":
            sm = (st.get("model") or "").strip()
            if sm and not any(k in sm.lower() for k in ("hydrogen", "orchestrator", "coordinator")):
                last_non_hydrogen_model = sm
                last_flow_model = sm
        elif stype == "GENERIC" and "Created the following subagent:" in content:
            for m_sub in uuid_re.finditer(content):
                spawned_subagents.append(m_sub.group(1))

    if last_flow_model:
        return last_flow_model
    if last_setting and any(k in last_setting.lower() for k in ("argon", "neon", "claude", "gpt")):
        return last_setting
    if last_non_hydrogen_model:
        return last_non_hydrogen_model

    # 2. Swarm members.json
    roster_path = session_dir / ".system_generated" / "swarm" / "members.json"
    if roster_path.exists():
        try:
            robj = json.loads(roster_path.read_text(encoding="utf-8", errors="replace"))
            members = robj.get("members") or {}
            for mkey in ["coordinator"] + [k for k in members.keys() if k != "coordinator"]:
                m_info = members.get(mkey)
                if isinstance(m_info, dict):
                    m_val = (m_info.get("model") or "").strip()
                    if m_val and "hydrogen" not in m_val.lower():
                        return m_val
        except Exception:
            pass

    # 3. 子サブエージェントのトランスクリプトから継承されたメインモデルを逆探知
    found_sub_models = []
    for sub_id in reversed(spawned_subagents[-6:]):
        sub_log = BRAIN_DIR / sub_id / ".system_generated" / "logs" / "transcript.jsonl"
        if not sub_log.exists():
            continue
        try:
            with sub_log.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"PLANNER_RESPONSE"' in line and '"model"' in line:
                        obj = json.loads(line)
                        sm = (obj.get("model") or "").strip()
                        if sm and "hydrogen" not in sm.lower():
                            found_sub_models.append(sm)
                            break
        except Exception:
            continue

    for sm in found_sub_models:
        if "argon" in sm.lower():
            return sm
    for sm in found_sub_models:
        if "neon" in sm.lower():
            return sm
    if found_sub_models:
        return found_sub_models[0]

    # 4. グローバル設定のデフォルトモデル
    return detect_global_default_model()


def load_titles_from_sqlite() -> dict:
    """conversation_summaries.db から最新のセッション名を一括取得（識別子クォート済み）"""
    if not SUMMARIES_DB_PATH.exists():
        return {}
    conn = None
    try:
        wal_path = Path(str(SUMMARIES_DB_PATH) + "-wal")
        mtime = max(
            SUMMARIES_DB_PATH.stat().st_mtime,
            wal_path.stat().st_mtime if wal_path.exists() else 0
        )
        if mtime == _DB_TITLES_CACHE["mtime"] and _DB_TITLES_CACHE["titles"]:
            return _DB_TITLES_CACHE["titles"]

        titles = {}
        conn = sqlite3.connect(f"file:{SUMMARIES_DB_PATH.as_posix()}?mode=ro", uri=True, timeout=1.5)
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for tname in tables:
            qt = _quote_ident(tname)
            cols = [c[1] for c in cur.execute(f"PRAGMA table_info({qt})").fetchall()]
            id_col = next((c for c in cols if c.lower() in ("conversation_id", "cascade_id", "id", "uuid", "session_id")), None)
            title_cols = [c for c in cols if any(k in c.lower() for k in ("title", "summary", "name"))]
            if id_col and title_cols:
                q_id = _quote_ident(id_col)
                q_tcols = ", ".join(_quote_ident(c) for c in title_cols)
                for row in cur.execute(f"SELECT {q_id}, {q_tcols} FROM {qt}").fetchall():
                    cid = str(row[0] or "").strip()
                    if not cid:
                        continue
                    for val in row[1:]:
                        if isinstance(val, str) and val.strip():
                            titles[cid] = val.strip()
                            break
            else:
                for row in cur.execute(f"SELECT * FROM {qt}").fetchall():
                    row_strs = [str(x) for x in row if x is not None]
                    cid = next((s for s in row_strs if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", s, re.I)), None)
                    if not cid:
                        continue
                    for s in row_strs:
                        if s == cid:
                            continue
                        if s.startswith("{") and s.endswith("}"):
                            try:
                                obj = json.loads(s)
                                t = obj.get("title") or obj.get("summary") or obj.get("name") or (obj.get("metadata") or {}).get("title")
                                if t:
                                    titles[cid] = str(t).strip()
                                    break
                            except Exception:
                                pass
        _DB_TITLES_CACHE["mtime"] = mtime
        _DB_TITLES_CACHE["titles"] = titles
        return titles
    except Exception:
        return _DB_TITLES_CACHE.get("titles", {})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def fetch_title_via_agentapi(conv_id: str, allow_spawn: bool = True) -> str:
    """agentapi get-conversation-metadata で最新のセッション名を取得（60秒キャッシュ）"""
    now = datetime.now().timestamp()
    cached = _TITLE_CACHE.get(conv_id)
    if cached and (now - cached[0] < 60.0) and cached[1]:
        return cached[1]
    if not allow_spawn:
        return cached[1] if cached else ""

    md = _run_agentapi_metadata(conv_id)
    title = _extract_title_from_metadata(md)
    if title:
        _TITLE_CACHE[conv_id] = (now, title)
        return title
    return cached[1] if cached else ""


def detect_session_specific_model(conv_id: str) -> str:
    """特定セッション専用のメタデータから現在選択中のモデル名を取得する（15秒キャッシュ）"""
    if not conv_id:
        return ""
    now = datetime.now().timestamp()
    cached = _SESSION_MODEL_CACHE.get(conv_id)
    if cached and (now - cached[0] < 15.0):
        return cached[1]

    md = _run_agentapi_metadata(conv_id)
    if md:
        title = _extract_title_from_metadata(md)
        if title:
            _TITLE_CACHE[conv_id] = (now, title)
        for candidate in (
            md.get("model"),
            md.get("selectedModel"),
            md.get("modelSelection"),
            (md.get("metadata") or {}).get("model"),
            (md.get("metadata") or {}).get("selectedModel"),
            (md.get("conversation") or {}).get("model"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                res = candidate.strip()
                _SESSION_MODEL_CACHE[conv_id] = (now, res)
                return res

    _SESSION_MODEL_CACHE[conv_id] = (now, "")
    return ""


def detect_global_default_model() -> str:
    """新規セッション向けに config.json からグローバルのデフォルトモデルを取得"""
    cfg_file = CONFIG_DIR / "config.json"
    try:
        if cfg_file.exists():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8", errors="replace"))
            for section in (cfg.get("userSettings") or {}, cfg.get("settings") or {}, cfg):
                if not isinstance(section, dict):
                    continue
                for key in ("modelSelection", "Model Selection", "model", "selectedModel", "defaultModel"):
                    val = section.get(key)
                    if isinstance(val, str) and val.strip() and val.strip().lower() != "none":
                        return val.strip()
                    elif isinstance(val, dict):
                        m_inner = val.get("model") or val.get("label") or val.get("name")
                        if isinstance(m_inner, str) and m_inner.strip():
                            return m_inner.strip()
    except Exception:
        pass
    return ""


def resolve_session_title(conv_id: str, fallback_title: str, allow_spawn: bool = False) -> str:
    """1. agentapi -> 2. conversation_summaries.db -> 3. トランスクリプト初期発言の順でタイトルを解決"""
    t = fetch_title_via_agentapi(conv_id, allow_spawn=allow_spawn)
    if t:
        return t
    db_titles = load_titles_from_sqlite()
    if conv_id in db_titles and db_titles[conv_id]:
        return db_titles[conv_id]
    return fallback_title


def get_subagent_ids() -> set:
    """サブエージェント（またはSwarmワーカー）のセッションIDを収集（15秒キャッシュ）"""
    now = datetime.now().timestamp()
    if _SUBAGENT_IDS_CACHE["ids"] and (now - _SUBAGENT_IDS_CACHE["timestamp"] < 15.0):
        return _SUBAGENT_IDS_CACHE["ids"]

    subagent_ids = set()
    if not BRAIN_DIR.exists():
        return subagent_ids

    for d in BRAIN_DIR.iterdir():
        if not d.is_dir():
            continue
        roster_file = d / ".system_generated" / "swarm" / "members.json"
        if roster_file.exists():
            try:
                robj = json.loads(roster_file.read_text(encoding="utf-8", errors="replace"))
                for mname, mdata in (robj.get("members") or {}).items():
                    if mname != "coordinator" and isinstance(mdata, dict):
                        cid = mdata.get("conversation_id")
                        if cid:
                            subagent_ids.add(cid)
            except Exception:
                pass
        tshort = d / ".system_generated" / "logs" / "transcript.jsonl"
        if tshort.exists():
            try:
                txt = tshort.read_text(encoding="utf-8", errors="replace")
                if "Created the following subagent:" in txt:
                    for m_sub in re.finditer(r'Created the following subagent:\s*\{\s*"conversationId":\s*"([0-9a-f\-]{36})"', txt):
                        subagent_ids.add(m_sub.group(1))
            except Exception:
                pass

    _SUBAGENT_IDS_CACHE["timestamp"] = now
    _SUBAGENT_IDS_CACHE["ids"] = subagent_ids
    return subagent_ids


def list_recent_sessions(limit: int = 15):
    """brainディレクトリ内の最新セッション一覧を高速取得（キャッシュ活用）"""
    if not BRAIN_DIR.exists():
        return []

    subagent_ids = get_subagent_ids()
    sessions = []
    for d in BRAIN_DIR.iterdir():
        if not d.is_dir() or d.name in subagent_ids:
            continue
        log_full = d / ".system_generated" / "logs" / "transcript_full.jsonl"
        log_short = d / ".system_generated" / "logs" / "transcript.jsonl"
        target_log = log_full if log_full.exists() else (log_short if log_short.exists() else None)
        if not target_log:
            continue
        try:
            stat = target_log.stat()
            cached_fb = _FALLBACK_TITLE_CACHE.get(d.name)
            if cached_fb and cached_fb[0] == stat.st_mtime:
                fallback_title = cached_fb[1]
            else:
                fallback_title = d.name[:8]
                with target_log.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if '"USER_INPUT"' in line:
                            obj = json.loads(line)
                            content = obj.get("content", "")
                            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.S)
                            raw_title = m.group(1).strip() if m else content.strip()
                            raw_title = re.sub(r"\s+", " ", raw_title)
                            if raw_title:
                                fallback_title = raw_title[:36] + ("…" if len(raw_title) > 36 else "")
                            break
                _FALLBACK_TITLE_CACHE[d.name] = (stat.st_mtime, fallback_title)

            sessions.append({
                "id": d.name,
                "fallbackTitle": fallback_title,
                "title": fallback_title,
                "mtime": stat.st_mtime,
                "updatedAt": datetime.fromtimestamp(stat.st_mtime).strftime("%m/%d %H:%M:%S"),
                "logPath": str(target_log),
            })
        except Exception:
            continue
    sessions.sort(key=lambda x: x["mtime"], reverse=True)
    top_sessions = sessions[:limit]
    for idx, s in enumerate(top_sessions):
        s["title"] = resolve_session_title(s["id"], s["fallbackTitle"], allow_spawn=(idx == 0))
    return top_sessions


def count_environment_items():
    """読み込まれているスキル数をカウント"""
    skills_count = 0
    if BUILTIN_SKILLS_DIR.exists():
        skills_count += sum(1 for d in BUILTIN_SKILLS_DIR.iterdir() if d.is_dir() and (d / "SKILL.md").exists())
    user_skills = CONFIG_DIR / "skills"
    if user_skills.exists():
        skills_count += sum(1 for d in user_skills.iterdir() if d.is_dir() and (d / "SKILL.md").exists())
    return {
        "skillsCount": skills_count,
    }
