# -*- coding: utf-8 -*-
"""
命令行入口。

用法::

    python main.py check              # 校验数据包完整性
    python main.py config             # 显示当前配置
    python main.py list               # 列出 23 套门店
    python main.py status             # 查看 output/ 的生成进度
    python main.py run --limit 6      # 小批量试跑（前 6 张）
    python main.py run --store 01     # 只跑第 1 套门店
    python main.py run                # 全量 138 张
    python main.py web                # 启动网页版（默认 http://127.0.0.1:8000）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 保证 Windows 控制台能正常输出中文
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.config import PROVIDER_PRESETS, load_config
from app.core.logging import current_log_file, get_logger, setup_logging
from app.state.manifest_store import ManifestStore
from app.generation.orchestrator import Orchestrator
from app.providers import create_provider
from app.state.storage import Storage
from app.state.store_repo import StoreRepository

log = get_logger("main")

BANNER = r"""
  AI图片生成
  23 套门店 × 6 张 = 138 张 可选比例玻璃静电贴设计图
"""


# ---------------------------------------------------------------- 命令
def cmd_check(args: argparse.Namespace) -> int:
    repo = StoreRepository()
    stats = repo.stats()
    print("=" * 66)
    print("数据包校验")
    print("=" * 66)
    print(f"数据文件 : {stats['path']}")
    print(f"门店套数 : {stats['stores']} / {stats['expected_stores']}")
    print(f"提示词数 : {stats['images']} / {stats['expected_images']}")
    print("-" * 66)
    problems = repo.validate()
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        print(f"\n共 {len(problems)} 个问题")
        return 1
    print("  ✅ 全部校验通过")
    print("=" * 66)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.provider)
    print("=" * 66)
    print("当前配置")
    print("=" * 66)
    print(cfg.describe())
    problems = cfg.validate()
    print("-" * 66)
    if problems:
        for p in problems:
            print(f"  ⚠️  {p}")
    else:
        print("  ✅ 配置正常，可以开始生成")
    print("=" * 66)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = StoreRepository()
    stores = repo.stores
    print(f"{'序号':<5}{'门店名称':<16}{'主标题':<12}{'副标题':<20}{'配色主题'}")
    print("-" * 88)
    for s in stores:
        print(
            f"{s.folder_index:<5}{s.folder_name:<14}{s.main_title:<10}"
            f"{s.sub_title:<18}{s.color_theme}"
        )
    print("-" * 88)
    print(f"共 {len(stores)} 套门店，{sum(len(s.items) for s in stores)} 张图片")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.provider)
    repo = StoreRepository()
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)

    print(f"输出目录：{cfg.output_root}")
    print("-" * 78)
    print(f"{'序号':<5}{'门店':<16}{'已生成':<8}{'总计':<6}{'状态'}")
    print("-" * 78)

    total_done = 0
    for s in repo.stores:
        m = manifests.for_store(s.output_dir)
        done = sum(1 for it in s.items if m.succeeded(it.pic_index))
        total_done += done
        n = len(s.items)
        flag = "✅ 完成" if done == n else ("⬜ 未开始" if done == 0 else f"🔸 {done}/{n}")
        print(f"{s.folder_index:<5}{s.folder_name:<14}{done:<8}{n:<6}{flag}")

    print("-" * 78)
    print(f"进度：{total_done} / 138 张")
    files = storage.count_images()
    if files != total_done:
        print(f"⚠️  磁盘上实际 PNG 文件数为 {files}，与 manifest 记录不一致")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.provider)
    if cfg.provider == "flux_local":
        print("FLUX 本地模型只能执行固定的门店 01 / V8 / 6 张验证。请在网页点击“运行本地样图验证”。")
        return 2
    setup_logging()

    print(BANNER)
    print(cfg.describe())
    print("-" * 66)

    problems = cfg.validate()
    if problems:
        for p in problems:
            print(f"  ⚠️  {p}")
        if not cfg.is_mock:
            print("\n配置不完整，已中止。可先用 mock 跑通流程：python main.py run --provider mock")
            return 1

    repo = StoreRepository()
    issues = repo.validate()
    if issues:
        print("数据包校验失败：")
        for i in issues[:5]:
            print(f"  ❌ {i}")
        return 1

    storage = Storage(
        cfg.output_root, cfg.timestamp_prefix,
        whiten_bg=cfg.whiten_background, whiten_threshold=cfg.whiten_threshold,
    )
    storage.prepare_directories(repo.stores)
    manifests = ManifestStore(cfg.output_root)

    provider = create_provider(cfg)
    orch = Orchestrator(cfg, provider, repo, storage, manifests)

    def on_event(ev: dict) -> None:
        if ev["type"] == "job_success":
            pass  # 已在 orchestrator 内打日志
        elif ev["type"] == "run_finished":
            print("-" * 66)

    orch.on_event(on_event)

    async def _main() -> None:
        try:
            stats = await orch.run(
                store_indexes=args.store,
                limit=args.limit,
            )
        finally:
            await provider.close()
        s = stats.to_dict()
        print("=" * 66)
        print("生成结果")
        print("=" * 66)
        print(f"  总计    : {s['total']}")
        print(f"  成功    : {s['success']}")
        print(f"  跳过    : {s['skipped']}（此前已生成）")
        print(f"  失败    : {s['failed']}")
        print(f"  重试    : {s['retries']} 次")
        print(f"  调用    : {s['calls']} 次")
        print(f"  耗时    : {s['elapsed']:.1f} 秒")
        if s["aborted"]:
            print(f"  中止    : {s['aborted']}")
        if cfg.price_per_image:
            print(f"  成本    : ≈ ¥{s['calls'] * cfg.price_per_image:.2f}")
        print("=" * 66)
        if current_log_file():
            print(f"日志：{current_log_file()}")

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n已中断。再次运行可断点续跑。")
        return 130
    return 0


# ---------------------------------------------------------------- 启动护栏
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def _guard_public_binding(host: str, *, force: bool) -> bool:
    """监听非本机地址时的安全护栏（文档 §2.5「默认拒绝」/§4）。

    本应用早期是**无认证**的单机工具：一旦用 `--host 0.0.0.0` 暴露到网络，
    任何人访问该端口就等于拥有完整权限（可读 API Key、消耗付费额度、删批次）。
    因此：

      · 尚未创建任何账号  → **拒绝启动**（除非显式 --i-know-its-public）
      · 已有账号          → 允许（阶段 1 完成后具备登录认证）

    Returns:
        True 表示可以继续启动；False 表示应中止。
    """
    if (host or "").strip().lower() in _LOOPBACK_HOSTS:
        return True

    has_user = False
    try:
        from app.security.users import UserStore

        has_user = UserStore.default().has_any_user()
    except Exception:  # noqa: BLE001 - 安全模块不可用时按「无账号」处理
        has_user = False

    print("\n" + "!" * 68)
    print(f"  警告：正在监听非本机地址 {host} —— 服务将可被网络中的其它设备访问")
    print("!" * 68)

    if force:
        print("  已指定 --i-know-its-public，继续启动。")
        print("  请自行确保运行在可信网络，并已配置防火墙规则。\n")
        return True

    if not has_user:
        print("\n  ❌ 已拒绝启动。原因：")
        print("     · 当前没有任何账号，服务处于「无认证」状态")
        print("     · 暴露到网络等于任何人都能读取 API Key、消耗付费额度、删除批次")
        print("\n  请选择其一：")
        print("     1) 改用本机地址（推荐）：main.py web --host 127.0.0.1")
        print("     2) 先创建管理员账号（登录后即可安全对外提供服务）")
        print("     3) 确认环境可信时显式承担风险：追加 --i-know-its-public")
        print()
        return False

    print("  已检测到账号体系，继续启动。请确认已开启防火墙限制访问来源。\n")
    return True


def cmd_web(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("缺少依赖，请先执行：pip install -r requirements.txt")
        return 1

    if not _guard_public_binding(args.host, force=getattr(args, "i_know_its_public", False)):
        return 2

    print(BANNER)
    print(f"  网页版地址：http://{args.host}:{args.port}")
    print("  按 Ctrl+C 停止\n")
    uvicorn.run(
        "app.web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="AI图片生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--provider", "-p",
        choices=list(PROVIDER_PRESETS),
        help="临时覆盖 .env 中的服务商设置",
    )
    sub = p.add_subparsers(dest="command")

    sub.add_parser("check", help="校验数据包完整性").set_defaults(func=cmd_check)
    sub.add_parser("config", help="显示当前配置").set_defaults(func=cmd_config)
    sub.add_parser("list", help="列出 23 套门店").set_defaults(func=cmd_list)
    sub.add_parser("status", help="查看生成进度").set_defaults(func=cmd_status)

    r = sub.add_parser("run", help="执行批量生成")
    r.add_argument("--store", "-s", nargs="*", help="只跑指定门店序号，如 01 02")
    r.add_argument("--limit", "-l", type=int, default=0, help="最多生成多少张（试跑用）")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("web", help="启动网页版")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--reload", action="store_true", help="开发模式自动重载")
    w.add_argument(
        "--i-know-its-public",
        dest="i_know_its_public",
        action="store_true",
        help="监听非本机地址时，确认已了解风险（无账号体系时默认拒绝启动）",
    )
    w.set_defaults(func=cmd_web)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    if args.command != "web":
        setup_logging()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
