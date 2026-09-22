# -*- coding: utf-8 -*-
"""清理验证脚本留下的测试账号。

这些账号是 `tools/verify_*.py` 反复运行累积的（每次都新建一个探针用户
然后停用，但**不会删除**），长期下来会污染用户列表。

安全设计：
    · **默认只预览**（dry-run），必须显式 `--apply` 才真删
    · 只删**登录名匹配已知测试前缀** 且 **不含 admin 角色** 的账号
    · 永不删除 `admin` 本身
    · 保留最近创建的一小批（便于回溯最近一次验证）

用法：
    .\\.venv\\Scripts\\python.exe tools\\cleanup_test_users.py            # 预览
    .\\.venv\\Scripts\\python.exe tools\\cleanup_test_users.py --apply    # 执行
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.service import SecurityService  # noqa: E402

# 验证脚本创建的探针账号前缀（见 tools/verify_*.py）
#   e2e / pv / p3probe / p0probe …都是各验证脚本的探针用户
TEST_PREFIXES = ("e2e", "p0probe", "p3probe", "pv", "thumb",
                 "b1", "lowp", "verify", "probe")
# 只要登录名里含这些片段，也视为测试账号（覆盖 p3probe 之类的新前缀）
TEST_SUBSTRINGS = ("probe",)


def is_test_account(name: str) -> bool:
    n = (name or "").lower()
    return (any(n.startswith(p) for p in TEST_PREFIXES)
            or any(s in n for s in TEST_SUBSTRINGS))
# 保留最近 N 个（便于回溯最近一次验证结果）
KEEP_RECENT = 5


def main() -> int:
    apply = "--apply" in sys.argv

    svc = SecurityService()
    users = svc.users.list_users()

    candidates = []
    for u in users:
        name = u.login_name or ""
        if name == "admin":
            continue                      # 永不删除主管理员
        if "admin" in (u.roles or []):
            continue                      # 有管理员角色的不动
        if any(name.startswith(p) for p in TEST_PREFIXES) or is_test_account(name):
            candidates.append(u)

    # 按创建时间排序，保留最近几个
    candidates.sort(key=lambda u: getattr(u, "created_at", "") or "", reverse=True)
    keep = candidates[:KEEP_RECENT]
    drop = candidates[KEEP_RECENT:]

    print("=" * 74)
    print("测试账号清理" + ("（执行）" if apply else "（预览，未改动任何数据）"))
    print("=" * 74)
    print(f"  账号总数        : {len(users)}")
    print(f"  匹配测试前缀    : {len(candidates)}")
    print(f"  保留（最近 {KEEP_RECENT} 个）: {len(keep)}")
    print(f"  将删除          : {len(drop)}")

    if keep:
        print("\n  保留：")
        for u in keep:
            print(f"    · {u.login_name:<24} {u.status}")

    if drop:
        print("\n  待删除：")
        for u in drop[:20]:
            print(f"    · {u.login_name:<24} [{', '.join(u.roles or [])}] {u.status}")
        if len(drop) > 20:
            print(f"    … 其余 {len(drop) - 20} 个")

    if not apply:
        print("\n  ⏭  预览模式。确认无误后加 --apply 执行：")
        print("      .\\.venv\\Scripts\\python.exe tools\\cleanup_test_users.py --apply")
        print("=" * 74)
        return 0

    # 执行删除
    deleted = 0
    failed: list[str] = []
    for u in drop:
        try:
            svc.users.delete(u.id)
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{u.login_name}: {type(exc).__name__}")

    svc.audit.log(
        action="user.cleanup_test_accounts", module="system", result="success",
        actor_id="cli", actor_name="本机 CLI", actor_roles=["system"],
        target_type="user", target_id="",
        changes={"deleted": deleted, "failed": len(failed),
                 "kept_recent": KEEP_RECENT,
                 "via": "tools/cleanup_test_users.py"},
    )

    print(f"\n  ✅ 已删除 {deleted} 个测试账号")
    if failed:
        print(f"  ⚠️ 失败 {len(failed)} 个：{failed[:5]}")
    remaining = len(SecurityService().users.list_users())
    print(f"  剩余账号: {remaining}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
