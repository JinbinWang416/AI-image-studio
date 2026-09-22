# -*- coding: utf-8 -*-
"""
设置存储（`config/settings.json`）。

设计要点：

1. **Web 界面可直接读写** —— 不需要手工改 `.env`，也不需要重启服务；
2. **优先级**：环境变量 > `settings.json` > 内置预设
   （保留用环境变量强制覆盖的能力，适合服务器/CI 场景）；
3. **API Key 掩码保护** —— 对外只返回 `sk-abc****xyz`；
   保存时识别「掩码回传」，避免把星号串写进配置里把真 Key 覆盖掉。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import PACKAGE_ROOT, STATE_ROOT
from .providers.catalog import default_settings

ROOT = PACKAGE_ROOT
CONFIG_DIR = STATE_ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"


# ---------------------------------------------------------------- 工具
def deep_merge(base: dict, patch: dict) -> dict:
    """递归合并，patch 覆盖 base。"""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def mask_key(key: str) -> str:
    """把 API Key 转为掩码：`sk-abc123456789` → `sk-abc****6789`。"""
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return key[:6] + "****" + key[-4:]


def is_masked(value: str) -> bool:
    """判断一个字符串是否是掩码（而非真实 Key）。"""
    return bool(value) and "****" in value


# ---------------------------------------------------------------- 存储
class SettingsStore:
    """设置读写。线程内单例式使用即可。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or SETTINGS_FILE)
        self._data: dict | None = None

    # ------------------------------------------------------------ 读
    def load(self, reload: bool = False) -> dict:
        """读取完整配置（与默认值深度合并，保证字段齐全）。"""
        if self._data is not None and not reload:
            return self._data

        data = default_settings()
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = deep_merge(data, raw)
            except (json.JSONDecodeError, OSError):
                # 配置损坏 → 用默认值，不阻塞
                pass
        self._data = data
        return data

    def masked(self) -> dict:
        """给前端的版本：API Key 全部打码。"""
        data = json.loads(json.dumps(self.load()))     # 深拷贝
        for p in data.get("providers", {}).values():
            if isinstance(p, dict) and p.get("api_key"):
                p["api_key"] = mask_key(p["api_key"])
                p["has_key"] = True
            elif isinstance(p, dict):
                p["has_key"] = False
        optimizer = data.get("prompt_optimizer")
        if isinstance(optimizer, dict):
            if optimizer.get("api_key"):
                optimizer["api_key"] = mask_key(optimizer["api_key"])
                optimizer["has_key"] = True
            else:
                optimizer["has_key"] = False
        return data

    # ------------------------------------------------------------ 写
    def save(self, patch: dict[str, Any]) -> dict:
        """保存增量配置。会自动处理掩码回传。"""
        current = self.load()
        patch = self._clear_batch_on_output_root_change(patch, current)
        patch = self._merge_secrets(patch, current)
        merged = deep_merge(current, patch)
        merged["version"] = current.get("version", 1)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(self.path)          # 原子替换

        self._data = merged
        return merged

    def _clear_batch_on_output_root_change(self, incoming: dict, current: dict) -> dict:
        """用户更改输出根目录时，不把旧批次名带到新路径。"""
        patch = json.loads(json.dumps(incoming))
        new_output = patch.get("output")
        if not isinstance(new_output, dict) or "root" not in new_output:
            return patch
        new_root = str(new_output.get("root") or "").strip()
        old_root = str((current.get("output") or {}).get("root") or "").strip()
        if new_root != old_root:
            new_output["active_batch"] = ""
            new_output["active_batch_created_at"] = ""
        return patch

    def _merge_secrets(self, incoming: dict, current: dict) -> dict:
        """处理 API Key 的三种提交情形。

        - 值为掩码（含 ****）→ **保留原 Key**（前端只是回显）
        - 值为空字符串      → **清空 Key**
        - 其他             → **更新为新值**
        """
        patch = json.loads(json.dumps(incoming))       # 深拷贝，避免改到调用方数据
        in_providers = patch.get("providers")
        cur_providers = current.get("providers", {})
        if isinstance(in_providers, dict):
            for name, pcfg in in_providers.items():
                if not isinstance(pcfg, dict) or "api_key" not in pcfg:
                    continue
                val = pcfg.get("api_key", "")
                if is_masked(val):
                    pcfg["api_key"] = cur_providers.get(name, {}).get("api_key", "")

        optimizer = patch.get("prompt_optimizer")
        current_optimizer = current.get("prompt_optimizer", {})
        if isinstance(optimizer, dict) and "api_key" in optimizer:
            if is_masked(optimizer.get("api_key", "")):
                optimizer["api_key"] = current_optimizer.get("api_key", "")
        return patch

    # ------------------------------------------------------------ 运行时
    def provider_runtime(self, name: str | None = None) -> dict:
        """取某服务商的运行时配置（含**真实** Key）。

        环境变量优先于 settings.json。
        """
        import os

        data = self.load()
        name = name or data.get("active_provider", "mock")
        from .providers.catalog import PROVIDER_CATALOG

        catalog = PROVIDER_CATALOG.get(name, {})
        stored = data.get("providers", {}).get(name, {})

        env_key_name = catalog.get("env_key") or ""
        api_key = os.environ.get(env_key_name, "").strip() or stored.get("api_key", "")

        return {
            "provider": name,
            "api_key": api_key,
            "base_url": stored.get("base_url") or catalog.get("base_url", ""),
            "model": stored.get("model") or catalog.get("default_model", ""),
            "catalog": catalog,
        }

    def active_provider(self) -> str:
        return self.load().get("active_provider", "mock")

    def reset(self) -> dict:
        """恢复默认设置（保留已填的 API Key）。"""
        current = self.load()
        fresh = default_settings()
        for name, p in current.get("providers", {}).items():
            if p.get("api_key") and name in fresh["providers"]:
                fresh["providers"][name]["api_key"] = p["api_key"]
            if p.get("base_url") and name in fresh["providers"]:
                fresh["providers"][name]["base_url"] = p["base_url"]
        if current.get("prompt_optimizer", {}).get("api_key"):
            fresh["prompt_optimizer"]["api_key"] = current["prompt_optimizer"]["api_key"]
        self._data = fresh
        self.save({})
        return fresh


# ---------------------------------------------------------------- 单例
_store: SettingsStore | None = None


def get_store() -> SettingsStore:
    global _store
    if _store is None:
        _store = SettingsStore()
    return _store
