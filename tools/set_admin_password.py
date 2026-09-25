# -*- coding: utf-8 -*-
"""把 admin 账号密码改为指定值。

⚠️ 说明：
    密码强度规则要求 ≥10 位，而本次目标密码为 8 位 ——
    这是**用户明确指定**的，因此这里绕过强度校验直接写入哈希。
    同时会：
      · 撤销该账号全部旧会话（改密后旧登录必须失效）
      · 清空失败计数与锁定
      · 不要求下次登录改密（否则用户改的密码又被强制换掉）
      · 写审计

用法：
    .\\.venv\\Scripts\\python.exe tools\\set_admin_password.py <新密码>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.auth import hash_password, password_strength_error  # noqa: E402
from app.security.service import SecurityService  # noqa: E402


def main() -> int:
    # ⚠️ 必须显式挡掉 `-h` / `--help` / `/?` 这类参数：
    #    本脚本只检查「有没有第 2 个参数」，于是 `--help` 会被**当成新密码**写进去，
    #    把管理员密码改成字面量 `--help`（实测踩到，撤掉了旧会话且绕过了强度校验）。
    #    任何「参数即密码」的 CLI 都有这个陷阱。
    args = sys.argv[1:]
    if not args or args[0].strip() in ("-h", "--help", "/?", "help", "-?", "/help"):
        print("  用法: python tools/set_admin_password.py <新密码>")
        print()
        print("  ⚠️ 注意：参数会进入 shell 历史。更稳妥的是交互输入：")
        print('       $s = Read-Host -AsSecureString "新密码"')
        print('       $p = [System.Net.NetworkCredential]::new("", $s).Password')
        print('       .\\.venv\\Scripts\\python.exe tools\\set_admin_password.py $p')
        print('       Remove-Variable s, p')
        return 2
    new_pw = args[0]
    if new_pw.startswith("-"):
        print(f"  [拒绝] 密码不能以 '-' 开头（{new_pw!r} 看起来是命令行选项，不是密码）")
        return 2

    svc = SecurityService()

    # 找管理员账号（优先 login_name == admin，否则第一个 admin 角色）
    target = svc.users.get_by_login("admin")
    if target is None:
        admins = [u for u in svc.users.list_users() if "admin" in u.roles]
        if not admins:
            print("  ❌ 找不到管理员账号")
            return 1
        target = admins[0]

    print("=" * 70)
    print("修改管理员密码")
    print("=" * 70)
    print(f"  账号      : {target.login_name}（{target.display_name}）")
    print(f"  角色      : {', '.join(target.roles)}")

    # 强度提示（不阻断 —— 密码是用户指定的）
    warn = password_strength_error(new_pw, target.login_name)
    if warn:
        print(f"  ⚠️ 强度提示: {warn}")
        print("     （按用户指定继续设置）")

    svc.users.update_fields(
        target.id,
        password_hash=hash_password(new_pw),
        must_change_password=False,   # 不强制下次改密，否则又会被换掉
        failed_attempts=0,
        locked_until="",
        status="active",
    )

    revoked = svc.sessions.revoke_all_for_user(target.id)
    print(f"  已撤销旧会话: {revoked} 个")

    svc.audit.log(
        action="user.set_password_cli", module="system", result="success",
        actor_id="cli", actor_name="本机 CLI", actor_roles=["system"],
        target_type="user", target_id=target.id,
        changes={"login_name": target.login_name, "sessions_revoked": revoked,
                 "via": "tools/set_admin_password.py"},
    )

    # 验证
    fresh = svc.users.get(target.id)
    from app.security.auth import verify_password  # noqa: E402

    ok = verify_password(new_pw, fresh.password_hash)
    print(f"  验证新密码: {'✅ 可登录' if ok else '❌ 校验失败'}")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
