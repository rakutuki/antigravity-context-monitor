#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Monitor クォータ・Usage 取得モジュール
Language Server の RPC からモデル別使用量と残り時間を取得・整形します。
"""

import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DAEMON_DIR = Path.home() / ".gemini" / "antigravity" / "daemon"

_QUOTA_CACHE = {"timestamp": 0, "data": None, "status": "unavailable"}
_LS_ENDPOINTS_CACHE = {"timestamp": 0, "ttl": 30.0, "endpoints": None}
_WORKING_LS_ENDPOINT = None

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback_host(url: str) -> bool:
    """接続先 URL のホストがローカルループバックかどうかを判定する"""
    try:
        hostname = (urllib.parse.urlparse(url).hostname or "").lower()
        return hostname in _LOOPBACK_HOSTS
    except Exception:
        return False


def format_reset_duration(reset_time_str: str) -> str:
    """ISO 8601 UTC 文字列から '6d 9h' や '4h 39m' 形式の残り時間を算出"""
    if not reset_time_str or not isinstance(reset_time_str, str):
        return ""
    try:
        s = reset_time_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff_sec = int((dt - now).total_seconds())
        if diff_sec <= 0:
            return "soon"
        days = diff_sec // 86400
        hours = (diff_sec % 86400) // 3600
        mins = (diff_sec % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"
    except Exception:
        return ""


def discover_ls_endpoints():
    """Language Server の (address, csrf_token, scheme) 候補リストを取得（キャッシュ付き）"""
    now = datetime.now().timestamp()
    ttl = _LS_ENDPOINTS_CACHE.get("ttl", 30.0)
    if _LS_ENDPOINTS_CACHE["endpoints"] is not None and (now - _LS_ENDPOINTS_CACHE["timestamp"] < ttl):
        return _LS_ENDPOINTS_CACHE["endpoints"]

    candidates = []
    if _WORKING_LS_ENDPOINT:
        candidates.append(_WORKING_LS_ENDPOINT)

    env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if env_addr:
        candidates.append((env_addr, env_csrf, "http"))
        candidates.append((env_addr, env_csrf, "https"))

    # daemon ディレクトリ内にポート・CSRF情報ファイルがあれば読み取る
    try:
        if DAEMON_DIR.exists():
            for p in sorted(DAEMON_DIR.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
                if p.is_file():
                    try:
                        txt = p.read_text(encoding="utf-8", errors="replace").strip()
                        if txt.startswith("{"):
                            obj = json.loads(txt)
                            port = obj.get("port") or obj.get("httpPort") or obj.get("httpsPort")
                            csrf = obj.get("csrfToken") or obj.get("csrf_token") or env_csrf
                            if port:
                                candidates.append((f"127.0.0.1:{port}", csrf, "http"))
                                candidates.append((f"127.0.0.1:{port}", csrf, "https"))
                    except Exception:
                        pass
    except Exception:
        pass

    # 環境変数も daemon ファイルも見つからない場合のみプロセス引数を検索
    if not candidates:
        try:
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                cmd = [
                    "powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name LIKE 'language_server%'\" | Select-Object -ExpandProperty CommandLine"
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0, startupinfo=startupinfo)
                lines = (proc.stdout or "").splitlines()
            else:
                proc = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=1.5)
                lines = [line for line in (proc.stdout or "").splitlines() if "language_server" in line]

            for line in lines:
                m_csrf = re.search(r"--csrf_token[=\s]+([a-zA-Z0-9\-_]+)", line)
                csrf = m_csrf.group(1) if m_csrf else env_csrf
                for m_port in re.finditer(r"--(?:extension_server_port|port|http_port|https_port)[=\s]+(\d+)", line):
                    port = m_port.group(1)
                    candidates.append((f"127.0.0.1:{port}", csrf, "http"))
                    candidates.append((f"127.0.0.1:{port}", csrf, "https"))
        except Exception:
            pass

    found_any = len(candidates) > 0
    candidates.append(("127.0.0.1:5387", env_csrf, "http"))
    # 重複除去
    seen = set()
    uniq = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    _LS_ENDPOINTS_CACHE["timestamp"] = datetime.now().timestamp()
    _LS_ENDPOINTS_CACHE["ttl"] = 30.0 if found_any else 3.0
    _LS_ENDPOINTS_CACHE["endpoints"] = uniq
    return uniq


def call_ls_rpc(method_name: str, payload: dict = None):
    """Language Server の Connect RPC エンドポイントを呼び出す"""
    global _WORKING_LS_ENDPOINT
    body = json.dumps(payload or {}).encode("utf-8")

    # ローカルの自己署名証明書のみ許可するため、ループバック接続専用コンテキストを分ける
    loopback_ctx = ssl.create_default_context()
    loopback_ctx.check_hostname = False
    loopback_ctx.verify_mode = ssl.CERT_NONE
    standard_ctx = ssl.create_default_context()

    endpoints = [_WORKING_LS_ENDPOINT] if _WORKING_LS_ENDPOINT else discover_ls_endpoints()
    for ep in endpoints:
        if not ep:
            continue
        addr, csrf, scheme = ep
        url = f"{scheme}://{addr}/exa.language_server_pb.LanguageServerService/{method_name}"
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        }
        if csrf:
            headers["x-codeium-csrf-token"] = csrf
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        ssl_ctx = None
        if scheme == "https":
            ssl_ctx = loopback_ctx if _is_loopback_host(url) else standard_ctx

        try:
            with urllib.request.urlopen(req, timeout=1.2, context=ssl_ctx) as resp:
                if resp.status == 200:
                    _WORKING_LS_ENDPOINT = ep
                    raw = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw)
        except urllib.error.HTTPError as he:
            # 404 (Method not found) の場合はエンドポイント自体は生きている
            if he.code in (400, 404, 405):
                _WORKING_LS_ENDPOINT = ep
                return None
            continue
        except Exception:
            continue
    return None


def normalize_special_model_name(label: str) -> str:
    """
    'Gemini 4 Argon (Medium)' や 'Gemini 4 Argon (High)' などの思考レベル付きモデル名を
    'Gemini 4 Argon' のようにベースモデル名へ正規化して分裂を防ぐ。
    """
    s = (label or "").strip()
    s = re.sub(r"\s*\((?:low|medium|high|minimal|thinking|fast|standard|pro|flash)[^)]*\)\s*$", "", s, flags=re.I)
    if "argon" in s.lower():
        return "Gemini 4 Argon"
    return s.strip()


def fetch_usage_quotas() -> tuple:
    """
    Usage（Gemini Models / Claude and GPT models 等の Weekly limit & 5h limit）を取得。
    (groups_out, status) を返す。status は "ok" または "unavailable"。
    ok 時は10秒、unavailable 時は2秒キャッシュして早期リトライを可能にする。
    """
    now = datetime.now().timestamp()
    ttl = 10.0 if _QUOTA_CACHE["status"] == "ok" else 2.0
    if _QUOTA_CACHE["data"] is not None and (now - _QUOTA_CACHE["timestamp"] < ttl):
        return _QUOTA_CACHE["data"], _QUOTA_CACHE["status"]

    groups_out = []
    any_rpc_succeeded = False

    # 1. RetrieveUserQuotaSummary を試行
    res = call_ls_rpc("RetrieveUserQuotaSummary", {})
    raw_groups = None
    if isinstance(res, dict):
        any_rpc_succeeded = True
        raw_groups = (
            (res.get("response") or {}).get("groups")
            or res.get("groups")
            or (res.get("quotaSummary") or {}).get("groups")
        )

    if isinstance(raw_groups, list) and len(raw_groups) > 0:
        merged_groups = {}
        group_order = []
        for g in raw_groups:
            gname = (g.get("displayName") or g.get("name") or g.get("groupName") or "Models").strip()
            gn_lower = gname.lower()
            if "claude" in gn_lower or "gpt" in gn_lower:
                gname = "Claude and GPT models"
            elif "argon" in gn_lower:
                gname = "Gemini 4 Argon"
            elif "gemini" in gn_lower:
                gname = "Gemini Models"
            else:
                gname = normalize_special_model_name(gname)

            buckets = g.get("buckets") or g.get("limits") or []
            if gname not in merged_groups:
                merged_groups[gname] = {}
                group_order.append(gname)

            for b in buckets:
                w_raw = str(b.get("window") or b.get("type") or b.get("label") or "")
                w_lower = w_raw.lower()
                if "week" in w_lower or "7d" in w_lower:
                    w_label = "Weekly limit"
                elif "5h" in w_lower or "hour" in w_lower or "session" in w_lower:
                    w_label = "5h limit"
                else:
                    w_label = w_raw or "Weekly limit"

                rem_frac = b.get("remainingFraction")
                if rem_frac is None:
                    rem_frac = b.get("remaining_fraction", 1.0)
                pct_left = max(0, min(100, int(round(float(rem_frac) * 100))))
                reset_in = format_reset_duration(b.get("resetTime") or b.get("reset_time") or "")

                existing = merged_groups[gname].get(w_label)
                if existing is None or pct_left < existing["percentLeft"]:
                    merged_groups[gname][w_label] = {
                        "windowLabel": w_label,
                        "percentLeft": pct_left,
                        "resetIn": reset_in or (existing["resetIn"] if existing else ""),
                    }

        for gname in group_order:
            bmap = merged_groups[gname]
            items = sorted(
                bmap.values(),
                key=lambda x: 0 if "week" in x["windowLabel"].lower() else 1
            )
            if items:
                groups_out.append({
                    "groupName": gname,
                    "inlineSingle": len(items) == 1 and gname not in ("Gemini Models", "Claude and GPT models"),
                    "items": items,
                })

    # 2. RetrieveUserQuotaSummary でグループが取れなかった場合のみ GetUserStatus へフォールバック
    # （Argon 専用枠は廃止され Gemini Models に統合されたため、個別グループは生成しない）
    status_res = None
    if not groups_out and (_WORKING_LS_ENDPOINT is not None or isinstance(res, dict)):
        status_res = call_ls_rpc("GetUserStatus", {
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "locale": "ja"
            }
        })
    if isinstance(status_res, dict):
        any_rpc_succeeded = True
        user_status = status_res.get("userStatus") or status_res
        cascade_data = user_status.get("cascadeModelConfigData") or {}
        model_configs = cascade_data.get("clientModelConfigs") or []

        gemini_5h = None
        other_5h = None

        for mc in model_configs:
            label = (mc.get("label") or mc.get("modelOrAlias", {}).get("model") or "").strip()
            qinfo = mc.get("quotaInfo") or {}
            rem_frac = qinfo.get("remainingFraction")
            if rem_frac is None:
                continue
            pct_left = max(0, min(100, int(round(float(rem_frac) * 100))))
            reset_in = format_reset_duration(qinfo.get("resetTime") or "")
            ll = label.lower()

            if any(k in ll for k in ("gemini", "neon", "carbon", "argon", "hydrogen", "flash")):
                if gemini_5h is None or pct_left < gemini_5h[0]:
                    gemini_5h = (pct_left, reset_in)
            elif any(k in ll for k in ("claude", "gpt", "sonnet", "opus", "haiku", "o1", "o3", "o4")):
                if other_5h is None or pct_left < other_5h[0]:
                    other_5h = (pct_left, reset_in)

        if gemini_5h:
            groups_out.append({
                "groupName": "Gemini Models",
                "inlineSingle": False,
                "items": [{"windowLabel": "5h limit", "percentLeft": gemini_5h[0], "resetIn": gemini_5h[1]}]
            })
        if other_5h:
            groups_out.append({
                "groupName": "Claude and GPT models",
                "inlineSingle": False,
                "items": [{"windowLabel": "5h limit", "percentLeft": other_5h[0], "resetIn": other_5h[1]}]
            })

    status = "ok" if (any_rpc_succeeded or len(groups_out) > 0) else "unavailable"
    _QUOTA_CACHE["timestamp"] = datetime.now().timestamp()
    _QUOTA_CACHE["data"] = groups_out
    _QUOTA_CACHE["status"] = status
    return groups_out, status


def format_display_model_name(model_name: str) -> str:
    """
    内部モデル識別子を UI 表示用の分かりやすいモデル名に整形する。
    """
    s = (model_name or "").strip()
    if not s or s.lower() == "gemini":
        return "Gemini Models"
    s = re.sub(r"\s*\((?:xhigh|low|medium|high|minimal|thinking|fast|standard)[^)]*\)\s*$", "", s, flags=re.I)
    s = re.sub(r"[-_](?:xhigh|low|medium|high|minimal|thinking)$", "", s, flags=re.I)

    sl = s.lower()
    if "argon" in sl:
        return "Gemini 4 Argon"
    if "neon" in sl:
        if "orchestrator" in sl:
            return "Orchestrator (Gemini 4 Neon)"
        return "Gemini 4 Neon"
    if "hydrogen" in sl or "orchestrator" in sl or "coordinator" in sl:
        return "Orchestrator"
    if "-" in s or "_" in s:
        parts = re.split(r"[-_]+", s)
        formatted = []
        for p in parts:
            if p.lower() == "gpt":
                formatted.append("GPT")
            else:
                formatted.append(p.capitalize())
        return " ".join(formatted)
    return s


def select_active_model_usage(model_name: str, usage_groups: list, backing_model: str = ""):
    """
    現在のメインセッションで選択されているモデルに対応する Usage グループを特定し、
    Weekly limit と 5h limit のうち最も制限に近い方を1つ返す。
    """
    if not usage_groups:
        return None

    m_lower = (model_name or "").lower()
    b_lower = (backing_model or "").lower()
    is_orchestrator = "hydrogen" in m_lower or "orchestrator" in m_lower or "coordinator" in m_lower

    def find_group(predicate):
        for g in usage_groups:
            if predicate((g.get("groupName") or "").lower()):
                return g
        return None

    def most_constrained_item(grp):
        if not grp or not grp.get("items"):
            return None
        return min(grp["items"], key=lambda it: int(it.get("percentLeft", 100)))

    if is_orchestrator:
        gemini_grp = find_group(lambda gn: "gemini" in gn and "argon" not in gn) or usage_groups[0]
        argon_grp = find_group(lambda gn: "argon" in gn)
        claude_grp = find_group(lambda gn: "claude" in gn or "gpt" in gn)

        # Argon が明示されている場合のみ Argon 枠を使用
        if "argon" in m_lower or "argon" in b_lower:
            target_grp = argon_grp or gemini_grp
            best_it = most_constrained_item(target_grp)
            if not best_it:
                return None
            return {
                "groupName": target_grp.get("groupName", ""),
                "displayModelName": "Orchestrator (Gemini 4 Argon)",
                "modelName": model_name,
                "windowLabel": best_it.get("windowLabel", ""),
                "percentLeft": int(best_it.get("percentLeft", 100)),
                "resetIn": best_it.get("resetIn", ""),
            }

        # Claude / GPT 系がバックエンドモデルとして明示されている場合
        if any(k in b_lower for k in ("claude", "sonnet", "opus", "haiku", "gpt", "o1", "o3", "o4")):
            target_grp = claude_grp or gemini_grp
            best_it = most_constrained_item(target_grp)
            if not best_it:
                return None
            return {
                "groupName": target_grp.get("groupName", ""),
                "displayModelName": f"Orchestrator ({format_display_model_name(backing_model)})",
                "modelName": model_name,
                "windowLabel": best_it.get("windowLabel", ""),
                "percentLeft": int(best_it.get("percentLeft", 100)),
                "resetIn": best_it.get("resetIn", ""),
            }

        # Neon または未確定時は勝手に Argon 枠へフォールバックせず Gemini Models 枠を使用
        best_it = most_constrained_item(gemini_grp)
        if not best_it:
            return None
        disp_name = (
            "Orchestrator (Gemini 4 Neon)"
            if ("neon" in m_lower or "neon" in b_lower)
            else "Orchestrator"
        )
        return {
            "groupName": gemini_grp.get("groupName", ""),
            "displayModelName": disp_name,
            "modelName": model_name,
            "windowLabel": best_it.get("windowLabel", ""),
            "percentLeft": int(best_it.get("percentLeft", 100)),
            "resetIn": best_it.get("resetIn", ""),
        }

    display_model = format_display_model_name(model_name)
    matched_group = None

    if "argon" in m_lower:
        matched_group = find_group(lambda gn: "argon" in gn)
    elif any(k in m_lower for k in ("claude", "sonnet", "opus", "haiku", "gpt", "o1", "o3", "o4")):
        matched_group = find_group(lambda gn: "claude" in gn or "gpt" in gn)

    if not matched_group:
        matched_group = find_group(lambda gn: "gemini" in gn and "argon" not in gn) or usage_groups[0]

    most_constrained = most_constrained_item(matched_group)
    if not most_constrained:
        return None

    return {
        "groupName": matched_group.get("groupName", ""),
        "displayModelName": display_model,
        "modelName": model_name,
        "windowLabel": most_constrained.get("windowLabel", ""),
        "percentLeft": int(most_constrained.get("percentLeft", 100)),
        "resetIn": most_constrained.get("resetIn", ""),
    }
