# -*- coding: utf-8 -*-
"""下载并登记 FLUX.2 klein 4B 的离线运行所需权重。

这个脚本只在安装阶段联网；本地服务以 HF_HUB_OFFLINE=1 启动，绝不会在
生成时下载或调用远程 API。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

from convert_flux2_vae import convert


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_info(path: Path, root: Path) -> dict:
    return {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def git_commit(source: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=r"E:\AIModels\flux2-klein-4b")
    args = parser.parse_args()
    home = Path(args.home).resolve()
    weights = home / "weights"
    hf_home = home / "huggingface"
    source = home / "source"
    weights.mkdir(parents=True, exist_ok=True)
    hf_home.mkdir(parents=True, exist_ok=True)

    model = Path(hf_hub_download(
        repo_id="black-forest-labs/FLUX.2-klein-4B",
        filename="flux-2-klein-4b.safetensors",
        local_dir=str(weights),
    ))
    public_ae = Path(hf_hub_download(
        repo_id="black-forest-labs/FLUX.2-klein-4B",
        filename="vae/diffusion_pytorch_model.safetensors",
        local_dir=str(weights),
    ))
    ae = weights / "ae.safetensors"
    if not ae.exists():
        convert(public_ae, ae, source)

    # 官方实现使用模型 ID 加载 Qwen3 FP8 编码器；下载到本项目的 HF 缓存，
    # 以便服务在 TRANSFORMERS_OFFLINE 下也能完整启动。
    snapshot = Path(snapshot_download(
        repo_id="Qwen/Qwen3-4B-FP8",
        cache_dir=str(hf_home),
    ))

    text_files = [p for p in sorted(snapshot.rglob("*")) if p.is_file() and p.suffix in {".json", ".safetensors", ".bin", ".model"}]
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": "FLUX.2-klein-4b",
        "model_key": "flux.2-klein-4b",
        "license": "Apache-2.0",
        "source_repository": "https://github.com/black-forest-labs/flux2",
        "source_commit": git_commit(source),
        "weight_files": [file_info(model, home), file_info(ae, home)],
        "public_vae_source": file_info(public_ae, home),
        "text_encoder": {
            "repo_id": "Qwen/Qwen3-4B-FP8",
            "snapshot": str(snapshot.relative_to(home)),
            "files": [file_info(p, home) for p in text_files],
        },
        "runtime": {
            "python": sys.version.split()[0],
        },
    }
    try:
        import torch
        manifest["runtime"].update({"torch": torch.__version__, "cuda": torch.version.cuda})
    except Exception:  # noqa: BLE001
        pass
    target = home / "model_manifest.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)
    print(f"FLUX weight: {model}")
    print(f"AE weight: {ae}")
    print(f"Text encoder: {snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
