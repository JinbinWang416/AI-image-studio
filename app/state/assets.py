# -*- coding: utf-8 -*-
"""本地参考图片资产库。

参考图不写入设置或批次清单。浏览器将 data URL 交给本模块后，文件按 SHA-256
写到输出根目录的 ``_references`` 下；设置和批次仅保存稳定的资产 ID、摘要和
校验值。这样既可以复用同一组参考图，也不会在 manifest 中泄露 Base64 内容。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


MAX_BYTES = 10 * 1024 * 1024

# 参考图入库前的**自动压缩**上限（最长边像素）。
#
# 为什么需要：
#   · 图生图参考图以 **Base64 data URL** 传给服务商，体积会膨胀约 33%
#   · 一张 10MB 的图 → 13.3MB payload；多图生图 3 张 → 40MB
#   · 上传慢、请求大、有些服务商还会因 payload 过大直接拒绝
#
# 为什么 1536 够用：
#   图生图模型内部处理的多是 1024px 级别输入，超过这个边长的细节
#   本来就会被缩放掉，压缩几乎不影响生成效果，却能大幅减小体积。
#
# 压缩后**按新内容重新计算 SHA-256**，所以去重依然准确。
REFERENCE_MAX_EDGE = 1536

# 小于这个体积的图不压缩（省得做无意义的重编码）
_COMPRESS_MIN_BYTES = 400 * 1024

_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$", re.I)
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

# data_url() 的进程内缓存（按资产 ID）
#   只缓存 ≤2MB 的图，最多 8 张：大图缓存会让常驻内存迅速膨胀
_URL_CACHE: dict[str, str] = {}
_URL_CACHE_SIZE = 8
_URL_CACHE_MAX_BYTES = 2 * 1024 * 1024


def downscale_image(raw: bytes, mime_type: str,
                    max_edge: int = REFERENCE_MAX_EDGE) -> tuple[bytes, dict]:
    """把过大的参考图等比缩到 `max_edge` 以内。

    **判据是「边长」而不是「体积」**：

        · 边长超限 → 服务商可能**直接拒绝**（硬错误），必须缩
        · 体积大   → 只是上传慢（软问题）

    早先用「体积 < 400KB 就跳过」做前置判断，结果一张 3000×2000
    但只占 362KB 的图逃过了压缩 —— 边长照样超限。现在总是解码检查。

    Args:
        raw: 原始字节
        mime_type: `image/png` / `image/jpeg` / `image/webp`
        max_edge: 最长边上限

    Returns:
        ``(新字节, 信息字典)``。任何失败都**原样返回**（压缩是优化，不是必需路径）。
    """
    info: dict = {"applied": False, "bytes_before": len(raw), "bytes_after": len(raw)}
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            w, h = im.size
            info["from"] = [w, h]
            longest = max(w, h)
            if longest <= max_edge:
                info["reason"] = f"边长 {longest} 未超上限"
                return raw, info

            scale = max_edge / longest
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            resized = im.resize(new_size, Image.LANCZOS)
            info["to"] = list(new_size)

            buf = io.BytesIO()
            if mime_type == "image/png":
                # PNG 保留透明通道
                resized.save(buf, format="PNG", optimize=True)
            elif mime_type == "image/webp":
                resized.convert("RGB").save(buf, format="WEBP", quality=90, method=4)
            else:
                resized.convert("RGB").save(buf, format="JPEG", quality=90,
                                            optimize=True, progressive=True)

        out = buf.getvalue()
        info.update(applied=True, bytes_after=len(out),
                    reason=f"缩到 {new_size[0]}×{new_size[1]}")
        # 体积变大也照样采用 —— 尺寸是硬约束，体积只是成本。
        # 但记录一下，方便排障时知道"这张图缩了反而更大"。
        if len(out) >= len(raw):
            info["note"] = "重编码后体积略增，但因边长超限仍采用缩放结果"
        return out, info
    except Exception as exc:  # noqa: BLE001 - 压缩失败不影响上传
        info["reason"] = f"压缩跳过（{type(exc).__name__}）"
        return raw, info


class ReferenceAssetError(ValueError):
    """用户提交的参考图不符合图像服务商的公共约束。"""


@dataclass(frozen=True)
class ReferenceAsset:
    id: str
    sha256: str
    file_name: str
    mime_type: str
    bytes: int
    path: Path
    created_at: str
    # 附加元数据。用于区分「用户实拍」与「AI 生成」背景，并携带玻璃区域标定。
    # 典型键：kind / label / glass_region / prompt / provider / model / store_hint
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        data = {
            "id": self.id,
            "sha256": self.sha256,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "bytes": self.bytes,
            "created_at": self.created_at,
        }
        # 把附加元数据平铺出去，方便前端直接读 kind / glass_region
        if self.extra:
            data.update(self.extra)
            data["extra"] = dict(self.extra)
        return data

    def data_url(self) -> str:
        """返回 data URL（带进程内缓存）。

        ⚠️ 早先每次都 `read_bytes()` + base64，且**没有缓存** ——
           一个批次里同一张参考图会被编码几十次（每个 job 一次），
           10MB 的图每次都要读盘 + 编码出 13MB 字符串。

        缓存策略：只缓存小图（≤2MB），且最多 8 张 ——
        大图缓存会让常驻内存迅速膨胀，得不偿失。
        """
        cached = _URL_CACHE.get(self.id)
        if cached is not None:
            return cached

        encoded = base64.b64encode(self.path.read_bytes()).decode("ascii")
        url = f"data:{self.mime_type};base64,{encoded}"

        if self.bytes <= _URL_CACHE_MAX_BYTES:
            if len(_URL_CACHE) >= _URL_CACHE_SIZE:
                _URL_CACHE.pop(next(iter(_URL_CACHE)), None)   # 简单的 FIFO 淘汰
            _URL_CACHE[self.id] = url
        return url


class ReferenceAssetStore:
    """管理一个输出根目录下的、按校验值去重的图片资产目录。"""

    def __init__(self, output_base_root: Path | str, directory_name: str = "_references"):
        # ``directory_name`` 只由代码传入，不能来自浏览器，从而避免目录穿越。
        if directory_name not in {"_references", "_effect_backgrounds"}:
            raise ValueError("不支持的图片资产目录")
        self.root = Path(output_base_root).resolve() / directory_name

    def _meta_path(self, asset_id: str) -> Path:
        return self.root / f"{asset_id}.json"

    @staticmethod
    def _safe_name(value: str) -> str:
        value = Path(value or "参考图").name
        value = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", value, flags=re.UNICODE).strip("._")
        return value[:96] or "参考图"

    def _from_meta(self, data: dict) -> ReferenceAsset | None:
        try:
            asset_id = str(data["id"])
            mime = str(data["mime_type"])
            path = self.root / str(data["stored_name"])
            if not re.fullmatch(r"[a-f0-9]{16}", asset_id) or mime not in _EXT:
                return None
            resolved = path.resolve()
            resolved.relative_to(self.root.resolve())
            if not resolved.is_file():
                return None
            return ReferenceAsset(
                id=asset_id,
                sha256=str(data["sha256"]),
                file_name=str(data.get("file_name") or path.name),
                mime_type=mime,
                bytes=int(data["bytes"]),
                path=resolved,
                created_at=str(data.get("created_at") or ""),
                extra=dict(data.get("extra") or {}),
            )
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def list(self) -> list[ReferenceAsset]:
        if not self.root.exists():
            return []
        items: list[ReferenceAsset] = []
        for meta in sorted(self.root.glob("*.json"), key=lambda p: p.name, reverse=True):
            try:
                asset = self._from_meta(json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                asset = None
            if asset:
                items.append(asset)
        return items

    def get(self, asset_id: str) -> ReferenceAsset:
        if not re.fullmatch(r"[a-f0-9]{16}", asset_id or ""):
            raise ReferenceAssetError("参考图片标识无效")
        meta = self._meta_path(asset_id)
        try:
            asset = self._from_meta(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            asset = None
        if not asset:
            raise ReferenceAssetError(f"参考图片不存在或已被移动：{asset_id}")
        return asset

    def resolve_many(self, ids: list[str]) -> list[ReferenceAsset]:
        unique: list[str] = []
        for value in ids:
            text = str(value or "").strip()
            if text and text not in unique:
                unique.append(text)
        return [self.get(asset_id) for asset_id in unique]

    def add_data_url(
        self,
        data_url: str,
        file_name: str = "",
        extra: dict | None = None,
    ) -> ReferenceAsset:
        """保存一张图片资产。

        Args:
            extra: 附加元数据（如 ``{"kind": "ai_generated_background",
                   "glass_region": [0.03, 0.02, 0.95, 0.93]}``）。
                   仅写入本机 meta 文件，不含 Base64 内容。
        """
        match = _DATA_URL.fullmatch(str(data_url or "").strip())
        if not match:
            raise ReferenceAssetError("参考图仅支持 PNG、JPG/JPEG 或 WEBP 文件")
        mime_type = match.group(1).lower()
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ReferenceAssetError("参考图 Base64 数据无效") from exc
        if not raw:
            raise ReferenceAssetError("参考图为空")
        if len(raw) > MAX_BYTES:
            raise ReferenceAssetError("单张参考图不能超过 10MB")
        signatures = {
            "image/png": b"\x89PNG\r\n\x1a\n",
            "image/jpeg": b"\xff\xd8\xff",
            "image/webp": b"RIFF",
        }
        if not raw.startswith(signatures[mime_type]):
            raise ReferenceAssetError("参考图文件内容与声明格式不一致")

        # 过大的图先缩到 REFERENCE_MAX_EDGE 以内 ——
        # 参考图要以 Base64 传给服务商（体积 ×1.33），大图会让请求变得很重。
        raw, shrink = downscale_image(raw, mime_type)
        if shrink.get("applied"):
            compressed = True
        else:
            compressed = False

        digest = hashlib.sha256(raw).hexdigest()
        asset_id = digest[:16]
        ext = _EXT[mime_type]
        stored_name = f"{asset_id}{ext}"
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / stored_name
        if not target.exists():
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(target)

        meta_path = self._meta_path(asset_id)
        if meta_path.exists():
            try:
                existing = self._from_meta(json.loads(meta_path.read_text(encoding="utf-8")))
                if existing:
                    return existing
            except (OSError, json.JSONDecodeError):
                pass
        created_at = datetime.now().isoformat(timespec="seconds")
        meta = {
            "id": asset_id,
            "sha256": digest,
            "file_name": self._safe_name(file_name) or stored_name,
            "stored_name": stored_name,
            "mime_type": mime_type,
            "bytes": len(raw),
            "created_at": created_at,
        }
        if compressed:
            # 记录压缩前后，便于前端/排障确认"我传的图被缩了"
            meta["compressed"] = {
                "from": shrink.get("from"),
                "to": shrink.get("to"),
                "bytes_before": shrink.get("bytes_before"),
                "reason": shrink.get("reason"),
            }
        if extra:
            meta["extra"] = dict(extra)
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(meta_path)
        asset = self._from_meta(meta)
        if not asset:  # pragma: no cover - disk failure guard
            raise ReferenceAssetError("参考图保存后无法读取")
        return asset


class EffectBackgroundStore(ReferenceAssetStore):
    """真实门店玻璃照片资产库。

    它与图生图参考图隔离保存。效果图合成只在本机读取这些照片，不会把它们
    发送给任何图像服务商，也不会把 Base64 内容写进批次清单。
    """

    def __init__(self, output_base_root: Path | str):
        super().__init__(output_base_root, "_effect_backgrounds")


def validate_mode_assets(mode: str, assets: list[ReferenceAsset]) -> None:
    """校验三种图像模式的参考图数量。"""
    count = len(assets)
    if mode == "text":
        if count:
            raise ReferenceAssetError("文生图不能附带参考图，请切换为图生图或多图生图")
    elif mode == "image":
        if count != 1:
            raise ReferenceAssetError("图生图必须选择 1 张参考图")
    elif mode == "multi":
        if count < 2 or count > 3:
            raise ReferenceAssetError("多图生图必须选择 2–3 张参考图")
    else:
        raise ReferenceAssetError("未知图像模式")
