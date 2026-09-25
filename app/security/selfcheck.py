# -*- coding: utf-8 -*-
"""启动一致性自检（块 4-a）。

## 为什么需要

权限码散落在四处：

| 位置 | 作用 |
|------|------|
| `permissions.py` | **定义源**：合法权限码的全集 |
| `access_rules.py` | 路径 → 权限码 的映射 |
| `admin_routes.py` / `auth_routes.py` | 接口上的 `require_permission("...")` |
| `permissions.ROLE_TEMPLATES` | 角色模板引用的权限码 |

任何一处写错（拼错、改名后漏改）都会造成**静默故障**：

- 写了**不存在的权限码** → 该接口**永远 403**，没人能访问
- 路径映射**漏登记** → 接口退化为「只需登录」，**权限失效**
- 角色引用了不存在的码 → 该角色**永远拿不到这项权限**

这类问题不会报错、不会崩溃，只会「某天发现某人干不了某件事」。
因此在**启动时**就检查，发现不一致**直接拒绝启动**并列出差异。

## 应急

若确需跳过（例如线上紧急修数据）：设 `SHS_SKIP_SELFCHECK=1`。
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

__all__ = ["SelfCheckError", "check_permission_consistency", "run_startup_selfcheck"]

SKIP_ENV = "SHS_SKIP_SELFCHECK"


class SelfCheckError(RuntimeError):
    """启动自检未通过。"""


def _app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _codes_used_in_source(path: Path) -> set[str]:
    """从源码里提取 `require_permission("xxx")` 用到的权限码。"""
    out: set[str] = set()
    if not path.is_file():
        return out
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "require_permission":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and \
                isinstance(node.args[0].value, str):
            out.add(node.args[0].value)
    return out


def _codes_used_by_text(path: Path) -> set[str]:
    """兜底：用正则扫（应对动态构造的调用）。"""
    out: set[str] = set()
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for m in re.finditer(r'require_permission\(\s*["\']([A-Za-z0-9_.]+)["\']', text):
        out.add(m.group(1))
    return out


def check_permission_consistency() -> list[str]:
    """校验权限码是否四方一致。

    Returns:
        问题描述列表（空列表 = 通过）
    """
    root = _app_root()
    problems: list[str] = []

    from .permissions import PERMISSION_CODES, ROLE_TEMPLATES, role_default_permissions

    defined = set(PERMISSION_CODES)

    # ① access_rules.py 引用的权限码
    try:
        from ..web.access_rules import PATH_PERMISSION_RULES

        rule_codes = {p for _, _, p in PATH_PERMISSION_RULES if p}
        for code in sorted(rule_codes - defined):
            problems.append(
                f"access_rules.py 引用了未定义的权限码：{code}"
            )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"无法加载 access_rules.py：{exc}")

    # ② 接口里 require_permission 用到的权限码
    used: set[str] = set()
    for name in ("admin_routes.py", "auth_routes.py", "server.py"):
        p = root / "web" / name
        used |= _codes_used_in_source(p)
        used |= _codes_used_by_text(p)
    for code in sorted(used - defined):
        problems.append(f"接口引用了未定义的权限码：{code}")

    # ③ 角色模板引用的权限码
    for role in ROLE_TEMPLATES:
        for code in role_default_permissions(role):
            if code not in defined:
                problems.append(f"角色模板 {role} 引用了未定义的权限码：{code}")

    # ④ 反向检查：定义了、但既无路径映射也无接口引用的权限码。
    #
    #    判定为「真正悬空」的条件是**三者都不沾**：
    #      · 没有 access_rules 的路径映射
    #      · 没有接口的 require_permission 引用
    #      · 也没有被任何角色模板引用
    #
    #    只要角色模板引用了它，就说明它是**有意预留**的能力
    #    （例如 batch.delete / store.manage —— 角色里给了，但业务接口尚未实现），
    #    这种情况不算错误。
    referenced = rule_codes | used
    role_referenced: set[str] = set()
    for role in ROLE_TEMPLATES:
        role_referenced |= set(role_default_permissions(role))

    # 前端菜单/业务代码里会直接用到的权限（不经过路径映射）
    frontend_only = {"customer.contact.read"}

    silent = sorted(defined - referenced - role_referenced - frontend_only)
    if silent:
        problems.append(
            "以下权限码已定义，但既无路径映射、也无接口引用、任何角色也没有它"
            f"（确认是漏登记还是笔误）：{'、'.join(silent)}"
        )

    return problems


def run_startup_selfcheck(*, strict: bool = True) -> None:
    """服务启动时调用。

    Args:
        strict: True 时发现问题抛 ``SelfCheckError``

    Raises:
        SelfCheckError: 自检未通过且 strict=True
    """
    if os.environ.get(SKIP_ENV) == "1":
        import logging

        logging.getLogger("app.security").warning(
            "已通过 %s 跳过权限一致性自检 —— 仅在紧急情况下使用", SKIP_ENV
        )
        return

    problems = check_permission_consistency()
    if not problems:
        return

    detail = "\n".join(f"  · {p}" for p in problems)
    message = (
        "权限定义不一致，已拒绝启动：\n"
        f"{detail}\n\n"
        "修复方式：\n"
        "  1) 若权限码拼写有误 → 改回 permissions.py 中的正确码\n"
        "  2) 若是新增模块 → 在 permissions.py 加 Permission(...)，"
        "并在 access_rules.py 加一行路径映射\n"
        "  3) 紧急情况下可设 SHS_SKIP_SELFCHECK=1 跳过（不推荐）\n"
        "详见 docs/安全权限框架接入指南.md"
    )
    if strict:
        raise SelfCheckError(message)
    import logging

    logging.getLogger("app.security").error(message)
