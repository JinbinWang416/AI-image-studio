# -*- coding: utf-8 -*-
"""Convert the official public Diffusers FLUX.2 VAE to BFL core key names.

The klein-4B repository exposes its VAE in Diffusers layout while the BFL core
loader expects the legacy core layout. This is a key-name-only conversion: it
checks every tensor name and shape before writing the destination file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def core_key(diffusers_key: str) -> str:
    key = diffusers_key
    key = re.sub(r"^quant_conv\.", "encoder.quant_conv.", key)
    key = re.sub(r"^post_quant_conv\.", "decoder.post_quant_conv.", key)
    key = key.replace("encoder.conv_norm_out.", "encoder.norm_out.")
    key = key.replace("decoder.conv_norm_out.", "decoder.norm_out.")
    key = re.sub(r"^encoder\.down_blocks\.(\d+)\.resnets\.(\d+)\.", r"encoder.down.\1.block.\2.", key)
    key = re.sub(r"^encoder\.down_blocks\.(\d+)\.downsamplers\.0\.conv\.", r"encoder.down.\1.downsample.conv.", key)
    # Diffusers stores decoder blocks in the opposite order to the core model.
    key = re.sub(
        r"^decoder\.up_blocks\.(\d+)\.resnets\.(\d+)\.",
        lambda match: f"decoder.up.{3 - int(match.group(1))}.block.{match.group(2)}.",
        key,
    )
    key = re.sub(
        r"^decoder\.up_blocks\.(\d+)\.upsamplers\.0\.conv\.",
        lambda match: f"decoder.up.{3 - int(match.group(1))}.upsample.conv.",
        key,
    )
    for prefix in ("encoder", "decoder"):
        key = key.replace(f"{prefix}.mid_block.resnets.0.", f"{prefix}.mid.block_1.")
        key = key.replace(f"{prefix}.mid_block.resnets.1.", f"{prefix}.mid.block_2.")
        key = key.replace(f"{prefix}.mid_block.attentions.0.group_norm.", f"{prefix}.mid.attn_1.norm.")
        key = key.replace(f"{prefix}.mid_block.attentions.0.to_q.", f"{prefix}.mid.attn_1.q.")
        key = key.replace(f"{prefix}.mid_block.attentions.0.to_k.", f"{prefix}.mid.attn_1.k.")
        key = key.replace(f"{prefix}.mid_block.attentions.0.to_v.", f"{prefix}.mid.attn_1.v.")
        key = key.replace(f"{prefix}.mid_block.attentions.0.to_out.0.", f"{prefix}.mid.attn_1.proj_out.")
    return key.replace(".conv_shortcut.", ".nin_shortcut.")


def convert(source: Path, destination: Path, source_dir: Path) -> None:
    sys.path.insert(0, str(source_dir / "src"))
    from safetensors.torch import load_file, save_file
    from flux2.autoencoder import AutoEncoder, AutoEncoderParams

    expected = AutoEncoder(AutoEncoderParams()).state_dict()
    converted = {core_key(key): value for key, value in load_file(str(source)).items()}
    # Diffusers represents the mid-attention projections as Linear layers;
    # the BFL core represents the same weights as 1×1 Conv2d layers.
    for key, target in expected.items():
        value = converted.get(key)
        if value is not None and target.ndim == 4 and value.ndim == 2 and target.shape[:2] == value.shape:
            converted[key] = value[:, :, None, None]
    missing = sorted(set(expected) - set(converted))
    extra = sorted(set(converted) - set(expected))
    mismatched = sorted(
        key for key in expected.keys() & converted.keys()
        if expected[key].shape != converted[key].shape
    )
    if missing or extra or mismatched:
        raise RuntimeError(
            f"VAE conversion validation failed: missing={missing[:5]}, extra={extra[:5]}, shape={mismatched[:5]}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    save_file(converted, str(destination), metadata={"format": "pt", "source": "FLUX.2-klein-4B/vae"})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--source-dir", default=r"E:\AIModels\flux2-klein-4b\source")
    args = parser.parse_args()
    convert(Path(args.source), Path(args.destination), Path(args.source_dir))
