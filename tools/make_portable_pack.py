# -*- coding: utf-8 -*-
"""
打包成**可移植**的交付包，供 Codex 或其它环境继续开发。

## 包含
- 全部源码（app / main.py / tests / tools）
- 数据包（data/，23 套门店 × 6 主题）
- 文档（README / AGENTS / PROJECT_CONTEXT / docs/ / HANDOFF）
- 配置**模板**（config/settings.json 已脱敏，API Key 置空）
- 启动脚本（local-server.ps1 / *.vbs / requirements*.txt）
- AI 背景库（output/_effect_backgrounds，46 张，省去重新生成的费用）
- 一个示例批次的产出（生成图 + 效果图）

## 排除（及原因）
- `.venv/`            体积大，用 requirements.txt 重建
- `logs/` `_archive/` 日志与历史归档
- `output_local_*`    开发期验证产物
- `output/_references` **用户自有素材与第三方图片**（隐私 / 版权）
- 其余历史批次        体积大，非必需
- `.env`              **含 API Key，绝不打包**（本就无此文件）

> 注（2026-09-22）：`desktop/`、`delivery/`、根目录 `*.exe` 已按用户要求删除，
> 不再需要排除规则。构建脚本仍保留，需要时可重新生成。

用法：
    .\\.venv\\Scripts\\python.exe tools\\make_portable_pack.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAGE = ROOT / "_portable_stage"
OUT_DIR = ROOT / "delivery"

# 目录：源 → 包内路径
INCLUDE_DIRS: list[tuple[str, str]] = [
    ("app", "app"),
    ("tests", "tests"),
    ("tools", "tools"),
    ("data", "data"),
    ("docs", "docs"),
    ("config", "config"),
    ("output/_effect_backgrounds", "output/_effect_backgrounds"),
]

# 文件：源 → 包内路径
INCLUDE_FILES: list[str] = [
    "main.py",
    "README.md",
    "AGENTS.md",
    "PROJECT_CONTEXT.json",
    "DEEPSEEK_HANDOFF.md",
    "DEPLOYMENT-MODELS.md",
    "LOCAL-DEPLOYMENT.md",
    "local-server.ps1",
    ".env.example",
    ".gitignore",
    "requirements.txt",
    "requirements-local.lock.txt",
    "启动图片生成智能体.vbs",
    "停止图片生成智能体.vbs",
    "启动FLUX本地验证.vbs",
    "停止FLUX本地验证.vbs",
]

# 示例批次（取最新的一个含生成图+效果图的）
SAMPLE_BATCH_PREFIX = "batch_"

EXCLUDE_PATTERNS = (
    "__pycache__", ".pyc", ".pyo", ".tmp", ".bak", "_tmp", ".DS_Store", "Thumbs.db",
    # ⚠️ 账号与会话数据：含用户密码哈希与活跃登录令牌，
    #    绝不可随包外传；新环境首次启动会走 /api/auth/setup 重新初始化管理员。
    "data/security", "data\\security",
)


def pick_sample_batch() -> pathlib.Path | None:
    for batch in sorted((ROOT / "output").glob(f"{SAMPLE_BATCH_PREFIX}*"), reverse=True):
        if not batch.is_dir():
            continue
        for store in batch.iterdir():
            if not store.is_dir():
                continue
            gen = store / f"{store.name}生成图"
            if gen.is_dir() and any(p.stat().st_size > 100_000 for p in gen.glob("*.png")):
                return batch
    return None


def sanitize_settings(dest: pathlib.Path) -> int:
    """写出脱敏后的 settings.json，返回被清空的 key 数量。"""
    src = ROOT / "config" / "settings.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    cleared = 0
    providers = data.get("providers")
    if isinstance(providers, dict):
        for name, cfg in providers.items():
            if isinstance(cfg, dict) and cfg.get("api_key"):
                cfg["api_key"] = ""
                cleared += 1
    for key in ("prompt_optimizer_api_key", "deepseek_api_key"):
        if data.get(key):
            data[key] = ""
            cleared += 1
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "settings.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return cleared


def copy_tree(src: pathlib.Path, dst: pathlib.Path) -> tuple[int, int]:
    files = size = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        if any(x in str(p) for x in EXCLUDE_PATTERNS):
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        files += 1
        size += p.stat().st_size
    return files, size


def main() -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    print("=" * 78)
    print("打可移植包")
    print("=" * 78)

    total_files = total_size = 0

    # 目录
    for src_rel, dst_rel in INCLUDE_DIRS:
        src = ROOT / src_rel
        if not src.is_dir():
            print(f"  ⏭  {src_rel} 不存在")
            continue
        if src_rel == "config":
            # config 只放脱敏后的 settings.json
            dst = STAGE / dst_rel
            cleared = sanitize_settings(dst)
            n = 1
            total_files += n
            total_size += (dst / "settings.json").stat().st_size
            print(f"  ✅ config/settings.json（已脱敏，清空 {cleared} 处 Key）")
            continue
        n, sz = copy_tree(src, STAGE / dst_rel)
        total_files += n
        total_size += sz
        print(f"  ✅ {src_rel:<32} {n:>5} 文件  {sz/1024/1024:>7.2f} MB")

    # 文件
    for rel in INCLUDE_FILES:
        p = ROOT / rel
        if not p.is_file():
            continue
        target = STAGE / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        total_files += 1
        total_size += p.stat().st_size
    print(f"  ✅ 根级文件（{len(INCLUDE_FILES)} 个候选中实际复制完成）")

    # 示例批次
    sample = pick_sample_batch()
    if sample:
        n, sz = copy_tree(sample, STAGE / "output" / sample.name)
        total_files += n
        total_size += sz
        print(f"  ✅ 示例批次 {sample.name}")
        print(f"       {n} 文件  {sz/1024/1024:.2f} MB")

    # 打包说明
    readme = f"""# 可移植包说明

生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
来源：门店贴纸图片生成智能体（网页版）

## 快速开始

```powershell
# 1) 建虚拟环境并装依赖
python -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt

# 2) 填 API Key（config/settings.json 里的 api_key 字段，本包已清空）
#    或在网页「设置 → 模型服务」里填写并保存

# 3) 启动
.\\local-server.ps1
# 或
.\\.venv\\Scripts\\python.exe main.py web --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 —— **首次使用需在页面上初始化管理员账号**。

## 本包包含

| 目录 | 内容 |
|------|------|
| `app/` | 全部 Python 源码（服务端 / 渲染 / 服务商适配） |
| `tools/` | 维护与验证脚本（见下） |
| `tests/` | 单元测试（`python -m unittest discover -s tests`） |
| `data/` | 23 套门店数据（每套 6 个主题） |
| `docs/` | 设计与调参文档 |
| `output/_effect_backgrounds/` | 46 张 AI 生成的门店玻璃背景（23 行业各 2 张） |
| `output/<示例批次>/` | 一个完整批次的产出（设计图 + 效果图） |

## 本包**不包含**（及原因）

| 排除项 | 原因 |
|--------|------|
| `.env`、API Key | **密钥绝不外传**（settings.json 已脱敏，所有 api_key 置空） |
| `output/_references/` | 用户自有素材与第三方图片（隐私 / 版权） |
| `data/security/` | **用户账号与登录会话**（密码哈希 / 令牌）；新环境首次启动会引导重新初始化管理员 |
| `.venv/` | 体积大，用 `requirements.txt` 重建 |
| `desktop/` | 桌面版构建产物，已冻结 |
| `logs/`、`_archive/`、`output_local_*` | 日志与开发期验证产物 |
| 历史批次与 `*.exe` | 体积大，非必需 |

> **首次在新环境启动时**，由于未包含 `data/security/`，
> `/api/auth/status` 会返回 `needs_setup: true`，页面会引导创建管理员账号。
> 这是预期行为，不是故障。
>
> 另外注意：`/api/*` 业务接口**需要登录**（默认拒绝未登记的接口）。
> 命令行脚本若要读取 `/api/state`，需先登录并携带会话 cookie；
> 就绪检测请用公开接口 `/api/auth/status`。

## 常用工具脚本

```powershell
# 代码冗余自检（未使用导入 / 函数 / TODO）
.\\.venv\\Scripts\\python.exe tools\\selfcheck_code.py

# 生成某个行业的 AI 门店背景
.\\.venv\\Scripts\\python.exe tools\\gen_effect_background.py --store 房屋中介 --count 2

# 为 23 个行业各生成 2 张背景
.\\.venv\\Scripts\\python.exe tools\\gen_effect_background.py --all-stores --count 2

# 跑完整批次的效果图合成（走网页同款链路）
.\\.venv\\Scripts\\python.exe tools\\run_effect_batch.py

# 效果图参数接口验证
.\\.venv\\Scripts\\python.exe tools\\verify_effect_params.py

# 网页与批量流程的一致性验证（防止两条路径产出不一致）
.\\.venv\\Scripts\\python.exe tools\\verify_render_options_unified.py

# 生成交付包（对照图 + 验收清单 + zip）
.\\.venv\\Scripts\\python.exe tools\\make_delivery_pack.py
```

## 新接手者请先读

1. `AGENTS.md` —— 协作约定与**不可破坏的行为**
2. `PROJECT_CONTEXT.json` —— 机器可读的约束、入口与验证命令
3. `docs/效果图参数基准_真实产品实测.md` —— 效果图参数的**全部依据**
   （含 6 版背景提示词迭代、6 个已修 bug 的根因与教训）

## 重要约束（摘自 AGENTS.md）

- 批次**不可变**：「重新生成」必须新建目录，绝不覆盖旧批次。
- 效果图背景来源必须**明确标记**：`real_photo` / `ai_generated_background` / `simulated`
  —— AI 生成的背景**不得**当作实拍图使用或宣传。
- 测试**不得调用付费图像 API**（用 mock 或本机虚拟图片）。
- 解析逻辑只有一处：`app/effect_background.py::resolve_render_options()`
  —— 任何新的效果图调用点都必须走它，否则会重现"网页正常、批量产出错误"的问题。
"""
    (STAGE / "PORTABLE_README.md").write_text(readme, encoding="utf-8")
    total_files += 1

    # 压缩
    OUT_DIR.mkdir(exist_ok=True)
    zip_path = OUT_DIR / f"门店贴纸智能体_可移植包_{time.strftime('%Y%m%d')}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(STAGE.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(STAGE))

    print("-" * 78)
    print(f"暂存：{total_files} 文件，{total_size/1024/1024:.1f} MB")
    print(f"压缩包：{zip_path.relative_to(ROOT)}  "
          f"({zip_path.stat().st_size/1024/1024:.1f} MB)")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
