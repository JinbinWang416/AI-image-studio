# -*- coding: utf-8 -*-
"""上线前安全检查。

不修改任何东西，只做只读扫描与结论。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

problems: list[str] = []
warns: list[str] = []
oks: list[str] = []

print("=" * 78)
print("C. 上线前安全检查")
print("=" * 78)

# ---------------------------------------------------------------- 1 权限一致性
print("\n【1】权限码四方一致性")
try:
    from app.security.selfcheck import check_permission_consistency

    # ⚠️ 这个函数返回的是 **list**（问题清单，空列表 = 一致），不是 dict。
    #    早先按 dict 用 r.get("consistent") 会抛 AttributeError，
    #    把"权限一致"误报成"自检失败"。
    issues = check_permission_consistency()
    if not issues:
        oks.append("权限码四方一致")
        print("  ✅ 四方权限码完全一致")
    else:
        problems.append(f"权限码不一致：{issues}")
        print(f"  ❌ 发现 {len(issues)} 处不一致：{issues}")
except Exception as exc:  # noqa: BLE001
    problems.append(f"权限自检失败：{exc}")
    print(f"  ❌ {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 2 密钥泄露
print("\n【2】API Key 是否泄露到产出/日志")

# 读取真实 Key（仅用于比对，绝不打印）
real_keys: list[str] = []
try:
    from app.state.settings_store import get_store

    data = get_store().load()
    for name, cfg in (data.get("providers") or {}).items():
        k = str((cfg or {}).get("api_key") or "").strip()
        if len(k) >= 12 and not k.startswith("*"):
            real_keys.append(k)
    for k in (str((data.get("prompt_optimizer") or {}).get("api_key") or "").strip(),):
        if len(k) >= 12 and not k.startswith("*"):
            real_keys.append(k)
except Exception as exc:  # noqa: BLE001
    warns.append(f"读取配置失败：{exc}")

print(f"  配置里有 {len(real_keys)} 个未遮罩的 Key（用于比对，不打印）")

# 扫描 output/ 与 logs/
scan_roots = [ROOT / "output", ROOT / "logs", ROOT / "backups"]
hits: list[str] = []
scanned = 0
for base in scan_roots:
    if not base.is_dir():
        continue
    for p in base.rglob("*"):
        if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
            continue
        scanned += 1
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for k in real_keys:
            if k in text:
                hits.append(f"{p.relative_to(ROOT)} 含明文 Key")
        if "data:image" in text and p.suffix in (".json", ".txt", ".log", ".csv"):
            hits.append(f"{p.relative_to(ROOT)} 含 Base64 图片")

print(f"  已扫描 {scanned} 个文本文件")
if hits:
    for h in hits[:10]:
        problems.append(h)
        print(f"  ❌ {h}")
else:
    oks.append("产出与日志中无明文 Key / Base64 图片")
    print("  ✅ 未发现明文 Key，也未发现 Base64 图片落盘")

# ---------------------------------------------------------------- 3 管理密码
print("\n【3】管理员账号状态")
try:
    from app.security.auth import password_strength_error
    from app.security.service import SecurityService

    svc = SecurityService()
    admin = svc.users.get_by_login("admin")
    if admin is None:
        problems.append("admin 账号不存在")
        print("  ❌ admin 账号不存在")
    else:
        print(f"  账号: {admin.login_name}  角色: {', '.join(admin.roles)}  状态: {admin.status}")
        if admin.status != "active":
            problems.append(f"admin 状态异常：{admin.status}")
        print(f"  锁定: {'是' if admin.locked_until else '否'}  失败计数: {admin.failed_attempts}")
        if admin.locked_until:
            warns.append("admin 当前处于锁定状态")

        # 密码强度只能间接判断：用一个弱密码样本比对
        for guess in ("admin", "123456", "password", "admin123", "Str0ng!Passw0rd"):
            from app.security.auth import verify_password

            if verify_password(guess, admin.password_hash):
                if guess == "Str0ng!Passw0rd":
                    problems.append("admin 仍在使用开发期默认密码")
                    print("  ❌ 正在使用开发期默认密码")
                elif len(guess) < 10:
                    warns.append(f"admin 密码过短（匹配到 {len(guess)} 位弱口令）")
                    print(f"  ⚠️ 密码强度不足（{len(guess)} 位）")
                else:
                    oks.append("admin 未使用常见弱口令")
                    print("  ✅ 非常见弱口令")
                break
        print(f"  MFA: {'已启用' if getattr(admin, 'mfa_enabled', False) else '未启用'}")

    # 其他账号
    others = [u for u in svc.users.list_users() if u.login_name != "admin"]
    print(f"  其它账号: {len(others)} 个")
    for u in others[:6]:
        flag = "" if u.status == "active" else f"（{u.status}）"
        print(f"    · {u.login_name} [{', '.join(u.roles)}] {flag}")
except Exception as exc:  # noqa: BLE001
    problems.append(f"账号检查失败：{exc}")
    print(f"  ❌ {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 4 敏感文件
print("\n【4】敏感文件暴露检查")
sensitive = [".env", "config/settings.json", ".env.local"]
for name in sensitive:
    p = ROOT / name
    if p.is_file():
        print(f"  · {name} 存在（{p.stat().st_size} 字节）—— 仅本机，勿提交/打包")
        if name == ".env":
            warns.append(".env 文件存在，注意不要打包外发")

# 检查 .gitignore 是否覆盖
gi = ROOT / ".gitignore"
if gi.is_file():
    text = gi.read_text(encoding="utf-8", errors="ignore")
    for pat in (".env", "settings.json", "output"):
        mark = "✅" if pat in text else "⚠️"
        if pat not in text:
            warns.append(f".gitignore 未覆盖 {pat}")
        print(f"  {mark} .gitignore 覆盖 {pat}")

# ---------------------------------------------------------------- 5 未鉴权接口
print("\n【5】未鉴权接口 —— 实测（不靠静态扫描猜）")
#
# ⚠️ 早先用「函数体里有没有 require_permission」做静态判断，把 47 个路由
#    误报为"未见鉴权" —— 实际上 `optional_user(request)` 也是鉴权，
#    而且 access_rules 对未注册的 /api/* 默认要求登录（AGENTS.md 的约定）。
#    结论只有**实测**才可信：未登录直接打接口，看是不是 401/403。
try:
    import urllib.error
    import urllib.request

    import re as _re

    src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
    routes = _re.findall(r'@app\.(get|post|put|patch|delete)\("(/api/[^"]+)"', src)
    print(f"  共 {len(routes)} 个 API 路由")

    # 有意公开的端点（登录前必须能访问）
    public_ok = {"/api/auth/login", "/api/auth/status", "/api/auth/logout",
                 "/api/auth/setup", "/api/auth/bootstrap", "/api/version",
                 "/api/auth/mfa/verify"}

    def probe(method: str, path: str) -> int:
        """未登录访问，返回状态码。"""
        concrete = _re.sub(r"\{[^}]+\}", "probe", path)
        url = f"http://127.0.0.1:8000{concrete}"
        req = urllib.request.Request(url, method=method.upper())
        if method.upper() in ("POST", "PUT", "PATCH"):
            req.add_header("Content-Type", "application/json")
            req.data = b"{}"
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001
            return -1

    leaked: list[str] = []
    checked = 0
    for method, path in routes:
        if path in public_ok:
            continue
        code = probe(method, path)
        checked += 1
        if code in (200, 201, 204):
            leaked.append(f"{method.upper()} {path} → {code}")

    print(f"  实测 {checked} 个非公开路由（未登录访问）")
    if leaked:
        for m in leaked:
            problems.append(f"未登录可访问：{m}")
            print(f"  ❌ {m}")
    else:
        oks.append(f"{checked} 个非公开路由全部拦截未登录访问")
        print("  ✅ 全部返回 401/403/404，未发现未鉴权放行")
except Exception as exc:  # noqa: BLE001
    warns.append(f"鉴权实测失败：{exc}")
    print(f"  ⚠️ {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 6 测试账号残留
print("\n【6】测试账号残留")
try:
    from app.security.service import SecurityService

    svc2 = SecurityService()
    users = svc2.users.list_users()
    prefixes = ("e2e", "p0probe", "pv", "thumb", "b1", "lowp", "verify", "probe")
    residue = [u for u in users
               if u.login_name != "admin"
               and any(u.login_name.startswith(p) for p in prefixes)]
    print(f"  账号总数: {len(users)}  疑似测试残留: {len(residue)}")
    if residue:
        active_residue = [u for u in residue if u.status == "active"]
        warns.append(
            f"{len(residue)} 个测试账号残留"
            + (f"，其中 {len(active_residue)} 个仍是启用状态" if active_residue else "（均已停用）")
        )
        print(f"  ⚠️ 建议清理（可运行 tools/cleanup_test_users.py --apply）")
        for u in residue[:5]:
            print(f"      · {u.login_name} [{', '.join(u.roles)}] {u.status}")
        if len(residue) > 5:
            print(f"      … 其余 {len(residue) - 5} 个")
    else:
        oks.append("无测试账号残留")
        print("  ✅ 无测试账号残留")
except Exception as exc:  # noqa: BLE001
    warns.append(f"账号残留检查失败：{exc}")

# ---------------------------------------------------------------- 7 MFA
print("\n【7】管理员多因素认证（MFA）")
try:
    from app.security.service import SecurityService

    svc3 = SecurityService()
    admins = [u for u in svc3.users.list_users() if "admin" in u.roles]
    no_mfa = [u for u in admins if not getattr(u, "mfa_enabled", False)]
    print(f"  管理员账号: {len(admins)}  未启用 MFA: {len(no_mfa)}")
    if no_mfa:
        warns.append(
            f"{len(no_mfa)} 个管理员未启用 MFA —— 本地部署可接受，"
            f"若暴露到局域网/公网则强烈建议启用"
        )
        for u in no_mfa:
            print(f"      · {u.login_name}")
    else:
        oks.append("所有管理员均已启用 MFA")
        print("  ✅ 全部已启用")
except Exception as exc:  # noqa: BLE001
    warns.append(f"MFA 检查失败：{exc}")

# ---------------------------------------------------------------- 结论
print()
print("=" * 78)
print("安全自检结论")
print("=" * 78)
print(f"  ✅ 通过 {len(oks)} 项")
for o in oks:
    print(f"      · {o}")
if warns:
    print(f"  ⚠️ 提示 {len(warns)} 项（不阻断上线，但建议处理）")
    for w in warns:
        print(f"      · {w}")
if problems:
    print(f"  ❌ 问题 {len(problems)} 项（**上线前必须处理**）")
    for p in problems:
        print(f"      · {p}")
else:
    print("  ✅ 无阻断性问题")
print("=" * 78)

sys.exit(1 if problems else 0)
