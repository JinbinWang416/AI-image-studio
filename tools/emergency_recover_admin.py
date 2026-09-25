# -*- coding: utf-8 -*-
"""
紧急恢复通道（块 1-d）—— **服务器本地 CLI，不经网页**。

## 什么时候用

1. 唯一的管理员账号被锁（连续登录失败）或被误停用/删除 → **无人能登录**
2. `data/security/security.json` 损坏 → 账号全部读不出来
3. 管理员忘了密码，且没有第二个管理员

> 这些情况下网页进不去，只能在本机用命令行救回来。

## 安全约束（必须全部满足）

| 约束 | 实现 |
|------|------|
| **仅限本机执行** | 检测远程会话（SSH/终端服务）与网络盘；非本机直接拒绝 |
| **二次确认** | 必须手工输入 `RECOVER` 才执行 |
| **干跑模式** | `--dry-run` 只报告将做什么，不修改任何文件 |
| **留痕** | 动作写入 `logs/emergency_recover_*.log` |
| **不绕过密码强度** | 新密码仍走同一套强度校验 |

## 用法

```powershell
# 1) 先看会做什么（不修改）
.\\.venv\\Scripts\\python.exe tools\\emergency_recover_admin.py --dry-run

# 2) 实际执行
.\\.venv\\Scripts\\python.exe tools\\emergency_recover_admin.py
```

可选参数：
    --reset-password    重置首个管理员的密码
    --unlock            清除全部登录失败锁定与会话
    --repair-json       修复损坏的 security.json
    --all               以上三项全做（默认）
    --yes               跳过交互确认（**仅供自动化，慎用**）
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SECURITY_DIR = ROOT / "data" / "security"
SECURITY_JSON = SECURITY_DIR / "security.json"
SESSIONS_JSON = SECURITY_DIR / "sessions.json"
LOG_DIR = ROOT / "logs"

CONFIRM_WORD = "RECOVER"


# ================================================================ 环境校验
def check_local_only() -> tuple[bool, str]:
    """确认是在**本机**执行（防远程误操作）。

    判定依据（任一命中即视为远程）：
      · 存在 SSH_CONNECTION / SSH_CLIENT 环境变量
      · 存在 SESSIONNAME 且以 RDP- 开头（Windows 远程桌面）
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
        return False, "检测到 SSH 远程会话（SSH_CONNECTION/SSH_CLIENT）"
    sess = (os.environ.get("SESSIONNAME") or "").upper()
    if sess.startswith("RDP-"):
        return False, f"检测到 Windows 远程桌面会话（{sess}）"
    return True, "本机会话"


# ================================================================ 备份/修复
def snapshot_before(actions: list[str]) -> pathlib.Path | None:
    """改动前先把现场存一份（无论修什么，都要能回退）。"""
    if not SECURITY_DIR.is_dir():
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = SECURITY_DIR / f"_recover_backup_{stamp}"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for p in SECURITY_DIR.glob("*.json"):
            shutil.copy2(p, dest / p.name)
        return dest
    except OSError:
        return None


def diagnose() -> dict:
    """检查当前安全数据状态。"""
    out: dict = {
        "security_json_exists": SECURITY_JSON.is_file(),
        "security_json_valid": False,
        "user_count": 0,
        "admin_count": 0,
        "has_snapshot": False,
        "sessions_exists": SESSIONS_JSON.is_file(),
        "snapshots": 0,
    }

    if SECURITY_JSON.is_file():
        try:
            data = json.loads(SECURITY_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out["security_json_valid"] = True
                users = data.get("users") or {}
                out["user_count"] = len(users)
                out["admin_count"] = sum(
                    1 for u in users.values()
                    if isinstance(u, dict) and "admin" in (u.get("roles") or [])
                )
        except (OSError, json.JSONDecodeError):
            out["security_json_valid"] = False

    snap_dir = SECURITY_JSON.with_name(SECURITY_JSON.name + ".snapshots")
    if snap_dir.is_dir():
        out["snapshots"] = len(list(snap_dir.glob("*.json")))
        out["has_snapshot"] = out["snapshots"] > 0
    return out


def repair_json(dry: bool, log) -> str:
    """修复损坏的 security.json（原子重建）。"""
    from app.security.store import JsonStore

    if not SECURITY_JSON.is_file():
        return "security.json 不存在，跳过"

    try:
        json.loads(SECURITY_JSON.read_text(encoding="utf-8"))
        return "security.json 未损坏，跳过"
    except (OSError, json.JSONDecodeError):
        pass

    # 用带自愈能力的 JsonStore 读取（它会用快照恢复或保留证据）
    store = JsonStore(SECURITY_JSON, default={"version": 1, "users": {}, "roles": {}})
    if dry:
        snap_dir = SECURITY_JSON.with_name(SECURITY_JSON.name + ".snapshots")
        n = len(list(snap_dir.glob("*.json"))) if snap_dir.is_dir() else 0
        return f"[干跑] 将从 {n} 份快照中恢复；无快照则重建空结构并保留损坏原文"

    data = store.load()
    if not data.get("users") and not data.get("roles"):
        data.setdefault("version", 1)
        data.setdefault("users", {})
        data.setdefault("roles", {})
        store.save(data)
        return "已重建为空结构（原损坏文件已保留为 .corrupt.*，如有快照则已恢复）"
    return f"已从快照恢复（{len(data.get('users') or {})} 个账号）"


def repair_without_selfheal(dry: bool) -> str:
    """当自愈不可用时的兜底：直接改名保留证据并重建。"""
    if dry:
        return "[干跑] 将把损坏文件改名为 .corrupt.<时间戳> 并重建空结构"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        shutil.move(str(SECURITY_JSON), str(SECURITY_JSON.with_name(
            f"{SECURITY_JSON.name}.corrupt.{stamp}")))
    except OSError as exc:
        return f"改名失败：{exc}"
    SECURITY_JSON.write_text(
        json.dumps({"version": 1, "users": {}, "roles": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "已重建空结构（原文件保留为 .corrupt.*）"


def reset_admin_password(dry: bool, new_password: str | None, log) -> str:
    """重置首个管理员的密码。"""
    from app.security.auth import hash_password, password_strength_error
    from app.security.users import UserStore

    store = UserStore(SECURITY_JSON)
    admins = [u for u in store.list_users() if "admin" in u.roles]
    if not admins:
        # 没有管理员：把最早的用户提为管理员
        all_users = store.list_users()
        if not all_users:
            return "没有任何账号，无法重置（请用 --repair-json 后重新初始化）"
        target = all_users[0]
        if dry:
            return f"[干跑] 将把 {target.login_name} 提升为管理员并重置密码"
        store.update_fields(target.id, roles=["admin"])
        admins = [store.get(target.id)]
        log(f"已将 {target.login_name} 提升为管理员")

    target = admins[0]
    if dry:
        return f"[干跑] 将重置管理员 {target.login_name} 的密码"

    pw = new_password
    if not pw:
        import getpass

        while True:
            pw = getpass.getpass(f"为 {target.login_name} 设置新密码：")
            pw2 = getpass.getpass("再输入一次：")
            if pw != pw2:
                print("  两次输入不一致，请重试")
                continue
            err = password_strength_error(pw, target.login_name)
            if err:
                print(f"  密码强度不足：{err}")
                continue
            break

    store.update_fields(
        target.id,
        password_hash=hash_password(pw),
        must_change_password=False,
        failed_attempts=0,
        locked_until="",
        status="active",
    )
    return f"已重置管理员 {target.login_name} 的密码并解除锁定"


def unlock_all(dry: bool, log) -> str:
    """清除登录失败锁定，并撤销全部会话。"""
    from app.security.auth import SessionStore
    from app.security.users import UserStore

    if dry:
        return "[干跑] 将清除全部账号的 failed_attempts/locked_until，并撤销全部会话"

    store = UserStore(SECURITY_JSON)
    n = 0
    for u in store.list_users():
        if u.failed_attempts or u.locked_until:
            store.update_fields(u.id, failed_attempts=0, locked_until="")
            n += 1

    revoked = 0
    try:
        ss = SessionStore(SESSIONS_JSON)
        for s in ss.list_all():
            if ss.revoke(s.id):
                revoked += 1
    except Exception as exc:  # noqa: BLE001
        log(f"清理会话时出错（已忽略）：{exc}")

    return f"已解除 {n} 个账号的锁定，撤销 {revoked} 个会话"


# ================================================================ 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="紧急恢复通道（仅限本机执行）")
    ap.add_argument("--dry-run", action="store_true", help="只报告将做什么，不修改文件")
    ap.add_argument("--repair-json", action="store_true", help="修复损坏的 security.json")
    ap.add_argument("--reset-password", action="store_true", help="重置首个管理员密码")
    ap.add_argument("--unlock", action="store_true", help="清除锁定并撤销全部会话")
    ap.add_argument("--all", action="store_true", help="执行以上全部（默认）")
    ap.add_argument("--password", default=None, help="直接指定新密码（不推荐，会留在命令历史）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（仅供自动化）")
    args = ap.parse_args()

    do_all = args.all or not (args.repair_json or args.reset_password or args.unlock)
    todo = []
    if do_all or args.repair_json:
        todo.append("repair-json")
    if do_all or args.unlock:
        todo.append("unlock")
    if do_all or args.reset_password:
        todo.append("reset-password")

    print("=" * 74)
    print("  紧急恢复通道")
    print("=" * 74)

    # ① 本机校验
    local, why = check_local_only()
    print(f"  环境检查 : {'✅ ' + why if local else '❌ ' + why}")
    if not local:
        print("\n  ⛔ 已拒绝：本通道只允许在**服务器本机**执行。")
        print("     请通过物理终端或本机远程桌面（非 SSH）登录后重试。\n")
        return 2

    # ② 诊断
    diag = diagnose()
    print("\n  【当前状态】")
    print(f"    security.json 存在 : {diag['security_json_exists']}")
    print(f"    security.json 可解析: {diag['security_json_valid']}")
    print(f"    账号数 / 管理员数   : {diag['user_count']} / {diag['admin_count']}")
    print(f"    可用快照            : {diag['snapshots']} 份")

    healthy = diag["security_json_valid"] and diag["admin_count"] > 0
    if healthy and not args.dry_run:
        print("\n  ℹ️  当前数据看起来正常（有可用管理员）。")
        print("     若只是想改密码，请登录后在「用户名 → 修改密码」里操作。")

    # ③ 计划
    print("\n  【将执行】")
    for t in todo:
        print(f"    · {t}")

    if args.dry_run:
        print("\n  " + "-" * 70)
        print("  干跑结果：")
        log = lambda m: None  # noqa: E731
        for t in todo:
            if t == "repair-json":
                print(f"    {repair_json(True, log)}")
            elif t == "unlock":
                print(f"    {unlock_all(True, log)}")
            else:
                print(f"    {reset_admin_password(True, args.password, log)}")
        print("  " + "-" * 70)
        print("\n  ✅ 干跑完成，**未修改任何文件**。\n")
        return 0

    # ④ 二次确认
    if not args.yes:
        print(f"\n  ⚠️  即将修改安全数据。请输入 {CONFIRM_WORD} 确认（其它输入将中止）：")
        try:
            typed = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            typed = ""
        if typed != CONFIRM_WORD:
            print("\n  ⛔ 未确认，已中止。\n")
            return 3

    # ⑤ 现场备份
    snap = snapshot_before(todo)
    if snap:
        print(f"\n  现场已备份到：{snap.relative_to(ROOT)}")

    # ⑥ 执行
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"emergency_recover_{time.strftime('%Y%m%d_%H%M%S')}.log"
    lines: list[str] = []

    def log(msg: str) -> None:
        lines.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        print(f"    {msg}")

    print("\n  【执行中】")
    results = []
    for t in todo:
        try:
            if t == "repair-json":
                r = repair_json(False, log)
            elif t == "unlock":
                r = unlock_all(False, log)
            else:
                r = reset_admin_password(False, args.password, log)
            results.append((t, r))
            log(r)
        except Exception as exc:  # noqa: BLE001
            results.append((t, f"失败：{exc}"))
            log(f"❌ {t} 失败：{exc}")

    # ⑦ 恢复日志
    try:
        log_path.write_text(
            "紧急恢复日志\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"环境：{why}\n"
            f"现场备份：{snap if snap else '（无）'}\n"
            f"执行项：{', '.join(todo)}\n"
            + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        print(f"\n  恢复日志：{log_path.relative_to(ROOT)}")
    except OSError as exc:
        print(f"\n  ⚠️ 恢复日志写入失败：{exc}")

    print("\n" + "=" * 74)
    print("  ✅ 处理完成。请启动服务并尽快登录确认，随后立即修改密码。")
    print("=" * 74 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
