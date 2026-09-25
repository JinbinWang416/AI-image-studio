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
import logging
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import PACKAGE_ROOT, STATE_ROOT
from .providers.catalog import default_settings

log = logging.getLogger("app.settings")

ROOT = PACKAGE_ROOT
CONFIG_DIR = STATE_ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
AUDIT_LOG = STATE_ROOT / "logs" / "settings-writes.log"

# ⚠️ 真实配置路径的**固化副本**，专供守卫与审计判定使用。
#
#    不要图省事直接用 `SETTINGS_FILE` 做判定 —— 它是个模块全局，
#    `unittest.mock.patch.object(mod, "SETTINGS_FILE", tmp)` 一句话就能把它换掉。
#    一旦被换掉，一个指向**真实配置**的 store 就会「看起来不像真实配置」：
#    守卫提前 return、审计静默跳过，写入畅通无阻。实测就是这么丢的
#    `active_provider=qwen`（文件 mtime 对得上，审计日志里却一条记录都没有）。
_REAL_SETTINGS_PATH = (STATE_ROOT / "config" / "settings.json").resolve()

# 允许测试写真实配置的逃生开关（默认关闭）。
ALLOW_TEST_WRITE_ENV = "SHS_ALLOW_TEST_CONFIG_WRITE"


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


# ---------------------------------------------------------------- 写入审计
# 本模块自己的文件名；扫描调用栈时要跳过它们。
_SETTINGS_MODULE_FILES = {"settings.py", "settings_store.py"}


def _frame_is_test(filename: str) -> bool:
    """判断某个栈帧是否来自单元测试。"""
    p = (filename or "").replace("\\", "/").lower()
    return (
        "/unittest/" in p
        or p.endswith("/unittest")
        or "/tests/" in p
        or p.endswith("/tests")
    )


def _scan_stack() -> tuple[str, str]:
    """扫描调用栈，返回 `(最近业务调用者, 测试来源)`。

    测试来源为空字符串表示「非测试上下文」。

    ⚠️ 只切掉 `_scan_stack` 自己那一帧（`[:-1]`），**不要切两帧** ——
       `save()` 帧由下面的文件名过滤负责。早先写成 `[:-2]`，隐含假设
       「调用者一定是 `save()`」，结果直接调用时会把真正的用户测试帧一起切掉，
       审计日志里只剩下 unittest 框架自己的 `case.py:589 in _callTestMethod`，
       完全定位不到是哪个测试干的。

    ⚠️ 过滤用**精确文件名**而不是子串：`"settings.py" in path` 这种写法
       早晚会误伤 `test_settings_guard.py` 之类同名前缀的文件。
    """
    frames = traceback.extract_stack()[:-1]
    caller = ""
    test_src = ""
    for fr in reversed(frames):
        if Path(fr.filename or "").name in _SETTINGS_MODULE_FILES:
            continue
        if _frame_is_test(fr.filename):
            if not test_src:
                test_src = f"{Path(fr.filename).name}:{fr.lineno} in {fr.name}"
            continue
        if not caller:
            caller = f"{Path(fr.filename).name}:{fr.lineno} in {fr.name}"
    return caller, test_src


def _append_audit(record: dict) -> None:
    """把一条写入记录追加到审计日志（失败绝不影响主流程）。

    记录里**只有服务商名与批次名，不含任何 API Key 或图片数据**。
    """
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

        # 简单轮转：超过 512KB 只保留最后 200 行
        if AUDIT_LOG.stat().st_size > 512 * 1024:
            tail = AUDIT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            AUDIT_LOG.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 存储
class ConfigCorruptError(RuntimeError):
    """配置文件读不懂，**拒绝**用默认值覆盖它。

    ⚠️ 为什么必须拒绝，而不是「留档之后照写」：

       `load()` 解析失败时 `_data` 是**默认值**（其中 `active_provider="mock"`）。
       如果放行 `save()`，一次读取故障就会把用户的 provider、密钥引用、
       输出路径等设置**从活动配置里永久抹掉** —— 留档只是降低了人工恢复成本，
       并没有阻止业务中断；万一留档那一步也失败，原始字节就彻底没了。

       正确做法：拒绝写入 + 明确告诉调用方原文件在哪、怎么恢复。

    Attributes:
        path: 出问题的配置文件
        backup: 已留档的副本路径（留档失败时为 None）
        reason: 解析失败的具体原因
    """

    def __init__(self, path: Any, backup: Any = None, reason: str = "") -> None:
        self.path = path
        self.backup = backup
        self.reason = reason
        msg = f"配置文件损坏，已拒绝覆盖：{path}\n  原因：{reason or '解析失败'}"
        if backup:
            msg += f"\n  原文件已留档：{backup}"
        else:
            msg += "\n  ⚠️ 留档也失败了，请先手工备份该文件再重置"
        super().__init__(msg)


class SettingsStore:
    """设置读写。线程内单例式使用即可。"""

    def __init__(self, path: Path | str | None = None):
        """构造设置存储。

        ⚠️ **测试进程 + 没显式给路径 → 强制落到临时目录。**
           这是「结构上不可能污染」的那一层：哪怕某个测试忘了传路径，
           它也拿不到真实配置。要写真实路径必须显式传，而那会被守卫拦下。
        """
        if (
            path is None
            and _running_under_unittest()
            and os.environ.get(ALLOW_TEST_WRITE_ENV) != "1"
        ):
            path = Path(tempfile.mkdtemp(prefix="shs-test-config-")) / "settings.json"
        self.path = Path(path or SETTINGS_FILE)
        self._data: dict | None = None

    # ------------------------------------------------------------ 读
    def load(self, reload: bool = False) -> dict:
        """读取完整配置（与默认值深度合并，保证字段齐全）。"""
        if self._data is not None and not reload:
            return self._data

        data = default_settings()
        self.corrupt = False
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("配置根节点不是 JSON 对象")
                data = deep_merge(data, raw)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
                # ⚠️ 配置损坏时**不能静默回退默认值**。
                #
                #    默认值里 `active_provider = "mock"`，而这份 data 会被缓存下来，
                #    之后任何一次 `save()` 都拿它当 base 写回文件 ——
                #    于是一次「读取故障」就升级成「配置被永久覆盖成 mock」。
                #    本项目的 config/settings.json 正是这样被改坏过两次
                #    （PowerShell 写出 BOM 导致 json 读不动，紧接着 save 把 mock 落了盘）。
                #
                #    现在：把读不懂的文件原样留档 + 打上 corrupt 标记，
                #    让后续 save() 也知道自己在覆盖一份没能读懂的配置。
                self.corrupt = True
                self._corrupt_reason = f"{type(exc).__name__}: {exc}"
                self._quarantine()
        self._data = data
        return data

    def _quarantine(self) -> None:
        """把读不懂的配置原样留档，便于人工恢复。"""
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            keep = self.path.with_name(f"{self.path.name}.corrupt.{stamp}")
            shutil.copy2(self.path, keep)
            self._corrupt_backup = keep
            log.warning(
                "配置损坏，已留档 %s（原因：%s）",
                keep.name, getattr(self, "_corrupt_reason", ""),
            )
        except OSError:
            self._corrupt_backup = None

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
    def is_real_config(self) -> bool:
        """当前实例指向的是否是项目真实配置（而非测试临时文件）。

        ⚠️ 判定基准是**固化的 `_REAL_SETTINGS_PATH`**，不是 `SETTINGS_FILE` ——
           后者能被 `mock.patch.object` 换掉，换掉之后守卫和审计会一起失灵。
        """
        try:
            return self.path.resolve() == _REAL_SETTINGS_PATH
        except OSError:
            return False

    def _guard_test_write(self) -> None:
        """阻止单元测试写坏真实配置。

        ⚠️ 背景：本项目的 `config/settings.json` **两次**在跑测试后被改成
           `provider=mock`，而逐个文件复跑测试又完全无法复现（23/23 未改），
           说明触发点在某个「测试失败时的异常路径」上。

           与其继续猜，不如设一道**硬闸**：只要求调用栈里出现 `unittest`
           或 `tests/`，且写入目标就是真实配置 → 直接拒绝。
           测试要写配置请用 `SettingsStore(tmp_path)`，那是另一条路径，不受影响。
        """
        if not self.is_real_config():
            return
        caller, test_src = _scan_stack()
        if not test_src or os.environ.get(ALLOW_TEST_WRITE_ENV) == "1":
            return

        _append_audit({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": "BLOCKED",
            "caller": caller,
            "test_source": test_src,
        })
        raise RuntimeError(
            "拒绝在单元测试中写入真实配置 config/settings.json\n"
            f"  调用者：{caller}\n"
            f"  测试来源：{test_src}\n"
            "  修正：测试请改用 SettingsStore(临时路径)；"
            f"确需绕过可设环境变量 {ALLOW_TEST_WRITE_ENV}=1"
        )

    def save(self, patch: dict[str, Any]) -> dict:
        """保存增量配置。会自动处理掩码回传。"""
        self._guard_test_write()
        current = self.load()

        if getattr(self, "corrupt", False):
            # ⚠️ **拒绝**用默认值覆盖读不懂的配置 —— 详见 ConfigCorruptError 的说明。
            #    留档只是降低恢复成本；真正要防的是「一次读取故障 →
            #    用户的 provider / 密钥引用 / 输出路径被永久抹掉」。
            caller, _ = _scan_stack() if self.is_real_config() else ("", "")
            _append_audit({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "action": "SAVE_REJECTED_CORRUPT",
                "caller": caller,
                "reason": getattr(self, "_corrupt_reason", ""),
                "keys": sorted(patch.keys()),
            })
            raise ConfigCorruptError(
                self.path,
                getattr(self, "_corrupt_backup", None),
                getattr(self, "_corrupt_reason", ""),
            )

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

        # 审计：只记「谁改的」和两个非敏感摘要字段
        if self.is_real_config():
            caller, test_src = _scan_stack()
            active = str((merged.get("output") or {}).get("active_batch") or "")
            _append_audit({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "action": "WRITE",
                "caller": caller,
                "test_source": test_src,
                "provider": str(merged.get("active_provider") or ""),
                "active_batch": active,
                "keys": sorted(patch.keys()),
            })
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

    def reset(self, *, allow_corrupt: bool = False) -> dict:
        """恢复默认设置（保留已填的 API Key 与 Base URL）。

        ⚠️ 配置损坏时**默认也会拒绝** —— 因为 `load()` 拿到的是默认值，
           「重置」会把用户原有设置彻底抹掉。要真的重置损坏配置，
           必须显式传 `allow_corrupt=True`（Web 层有对应的确认接口），
           而且**留档成功是前提**：留档都失败了就不许动原文件。

        Raises:
            ConfigCorruptError: 配置损坏且未显式允许重置
        """
        current = self.load()
        if getattr(self, "corrupt", False):
            backup = getattr(self, "_corrupt_backup", None)
            if not allow_corrupt:
                raise ConfigCorruptError(
                    self.path, backup, getattr(self, "_corrupt_reason", "")
                )
            if backup is None:
                # 留档失败 → 不能拿默认值顶掉原始字节，先让用户手工备份
                raise ConfigCorruptError(
                    self.path, None,
                    f"留档失败，拒绝重置以免丢失原始内容（{getattr(self, '_corrupt_reason', '')}）",
                )

        fresh = default_settings()
        for name, p in current.get("providers", {}).items():
            if p.get("api_key") and name in fresh["providers"]:
                fresh["providers"][name]["api_key"] = p["api_key"]
            if p.get("base_url") and name in fresh["providers"]:
                fresh["providers"][name]["base_url"] = p["base_url"]
        if current.get("prompt_optimizer", {}).get("api_key"):
            fresh["prompt_optimizer"]["api_key"] = current["prompt_optimizer"]["api_key"]

        # 已经确认过要重置，解除标记后再走 save()
        self.corrupt = False
        self._data = fresh
        self.save({})
        return fresh


# ---------------------------------------------------------------- 单例
_store: SettingsStore | None = None


def _running_under_unittest() -> bool:
    """当前进程是否由 unittest / pytest 驱动。

    ⚠️ 只用于**决定单例的落盘位置**，不会改变任何生产行为：
       服务进程（`main.py web`）永远不会加载 unittest。
    """
    return "unittest" in sys.modules or "pytest" in sys.modules


def get_store() -> SettingsStore:
    """返回全局设置单例。

    ⚠️ **测试进程自动隔离** —— 这是 `config/settings.json` 被改成 `mock`
       的根因修复：

       测试会直接调用真实端点（如 `api_openai_house_regenerate`），而端点内部
       走的是 `get_store()`。此前单例一律指向真实配置，于是在测试里执行的
       `save()` / `reset()` 会**真的改写用户配置**；而 `default_settings()`
       的默认 `active_provider` 恰好是 `"mock"`，一旦 `load()` 读到损坏内容
       回退默认值，`save()` 就把 `provider=mock` 落了盘（实测两次踩到）。

       现在：只要进程里有 unittest，单例就落到临时目录，真实配置完全隔离。
       每个测试文件各自新建 `SettingsStore()`（不带参数）的写法仍由
       `_guard_test_write()` 兜底拦截。
    """
    global _store
    if _store is None:
        if _running_under_unittest() and os.environ.get(ALLOW_TEST_WRITE_ENV) != "1":
            base = Path(tempfile.mkdtemp(prefix="shs-test-config-"))
            _store = SettingsStore(base / "settings.json")
        else:
            _store = SettingsStore()
    return _store
