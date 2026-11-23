#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Monitor 設定永続化モジュール
--data_dir 配下の settings.json をアトミックに読み書きします。
"""

import json
import os
import tempfile
from pathlib import Path

DEFAULT_THRESHOLD = 400000
MIN_THRESHOLD = 10000
MAX_THRESHOLD = 10000000
VALID_LANGUAGES = ("auto", "ja", "en", "zh")

DEFAULT_SETTINGS = {
    "threshold": DEFAULT_THRESHOLD,
    "expanded": False,
    "usageCollapsed": True,
    "language": "auto",
}


def _normalize_language(value) -> str:
    """言語設定値を正規化する（不正値時は None を返す）"""
    if not isinstance(value, str):
        return None
    norm = value.strip().lower()
    if norm in VALID_LANGUAGES:
        return norm
    return None


def clamp_threshold(value, default: int = DEFAULT_THRESHOLD) -> int:
    """しきい値トークン数を安全な整数範囲に丸める"""
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < MIN_THRESHOLD:
        return MIN_THRESHOLD
    if parsed > MAX_THRESHOLD:
        return MAX_THRESHOLD
    return parsed


def _sanitize_settings(raw: dict) -> dict:
    """許可された設定キーのみを型安全に抽出・正規化する"""
    result = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return result

    if "threshold" in raw and raw["threshold"] is not None:
        result["threshold"] = clamp_threshold(raw["threshold"], DEFAULT_THRESHOLD)
    if "expanded" in raw and isinstance(raw["expanded"], bool):
        result["expanded"] = raw["expanded"]
    if "usageCollapsed" in raw and isinstance(raw["usageCollapsed"], bool):
        result["usageCollapsed"] = raw["usageCollapsed"]
    if "language" in raw:
        lang = _normalize_language(raw["language"])
        if lang is not None:
            result["language"] = lang
    return result


def load_settings(data_dir: Path) -> dict:
    """settings.json から設定を読み込む（存在しない・破損時はデフォルト値を返す）"""
    if not data_dir:
        return dict(DEFAULT_SETTINGS)
    settings_path = Path(data_dir) / "settings.json"
    if not settings_path.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        raw_text = settings_path.read_text(encoding="utf-8", errors="replace")
        raw_obj = json.loads(raw_text)
        return _sanitize_settings(raw_obj)
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(data_dir: Path, patch: dict) -> dict:
    """現在の設定に patch をマージし、一時ファイル経由でアトミックに保存する"""
    current = load_settings(data_dir)
    if isinstance(patch, dict):
        if "threshold" in patch and patch["threshold"] is not None:
            current["threshold"] = clamp_threshold(patch["threshold"], current["threshold"])
        if "expanded" in patch and isinstance(patch["expanded"], bool):
            current["expanded"] = patch["expanded"]
        if "usageCollapsed" in patch and isinstance(patch["usageCollapsed"], bool):
            current["usageCollapsed"] = patch["usageCollapsed"]
        if "language" in patch:
            lang = _normalize_language(patch["language"])
            if lang is not None:
                current["language"] = lang

    if not data_dir:
        return current

    target_dir = Path(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    settings_path = target_dir / "settings.json"

    fd, tmp_path = tempfile.mkstemp(prefix="settings_", suffix=".tmp", dir=str(target_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, settings_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return current
