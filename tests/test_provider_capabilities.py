# -*- coding: utf-8 -*-
"""服务商图生图能力测试。

背景（AGENTS.md）：
    图生图/多图生图必须**显式声明**是否支持，**不能静默降级**。

覆盖：
    1. 所有服务商都必须显式声明三个能力字段
    2. flux_local 明确不支持（曾静默丢弃参考图）
    3. image_mode_error 的各条判定分支
    4. 能力一致：声明 supports_image 就必须有 image 模式
    5. 各服务商 generate() 里确实调用了统一校验
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.providers import get_provider  # noqa: E402
from app.providers.base import BaseProvider  # noqa: E402
from app.providers.catalog import PROVIDER_CATALOG  # noqa: E402

# 有实现、且应该支持图生图的服务商
IMAGE_PROVIDERS = {"qwen", "openai", "gemini", "seedream", "custom"}
# 明确不支持图生图的
TEXT_ONLY_PROVIDERS = {"flux_local", "mock"}


def make(name: str) -> BaseProvider:
    """构造一个 Provider 实例（**不联网**）。

    base_url 用 loopback：`flux_local` 在 `__init__` 里会强制校验
    只允许 127.0.0.1，用假域名会直接抛 LOCAL_ONLY。
    """
    return get_provider(name)(
        api_key="dummy",
        base_url="http://127.0.0.1:8189",
        model="",
    )


def try_make(name: str) -> BaseProvider | None:
    """尽力构造；**只有配置没有实现**的服务商返回 None。

    注意：`kling` / `zhipu` 在 PROVIDER_CATALOG 里有条目，但
    `app/providers/kling.py` / `zhipu.py` 并不存在，`get_provider()`
    会抛 `ModuleNotFoundError`（不是 ValueError）。这不是本模块的问题，
    测试里跳过即可 —— 但能力接口会如实把它们标成 `available: false`。
    """
    try:
        return make(name)
    except Exception:  # noqa: BLE001 - ValueError / ModuleNotFoundError
        return None


class TestDeclarations(unittest.TestCase):
    """1 + 4：声明完整性与一致性。"""

    def test_all_catalog_providers_declare_capabilities(self) -> None:
        """每个**有实现**的服务商类都必须显式声明三个能力字段。"""
        missing: list[str] = []
        checked = 0
        for name in PROVIDER_CATALOG:
            try:
                cls = get_provider(name)
            except Exception:  # noqa: BLE001 - 只有配置没有实现（kling / zhipu）
                continue
            checked += 1
            for field in ("supports_image", "supports_multi_image", "max_references"):
                if field not in vars(cls):
                    missing.append(f"{name}.{field}")
        self.assertGreater(checked, 5, f"只检查到 {checked} 个服务商，偏少")
        self.assertFalse(
            missing,
            "以下能力字段没有在服务商类里显式声明（落到了基类默认值）："
            + ", ".join(missing),
        )

    def test_declared_capabilities_are_consistent(self) -> None:
        """supports_image 与 max_references 必须自洽。"""
        for name in PROVIDER_CATALOG:
            p = try_make(name)
            if p is None:
                continue
            if p.supports_image:
                self.assertGreater(
                    p.max_references, 0,
                    f"{name} 声明支持图生图，但 max_references=0")
            else:
                self.assertEqual(
                    p.max_references, 0,
                    f"{name} 声明不支持图生图，但 max_references={p.max_references}")
            if p.supports_multi_image:
                self.assertTrue(
                    p.supports_image,
                    f"{name} 声明支持多图生图，却没声明支持图生图")

    def test_image_capabilities_modes(self) -> None:
        """image_capabilities() 的 modes 与布尔声明一致。"""
        for name in PROVIDER_CATALOG:
            p = try_make(name)
            if p is None:
                continue
            caps = p.image_capabilities()
            modes = caps["modes"]
            self.assertIn("text", modes, f"{name} 缺少 text 模式")
            self.assertEqual(
                "image" in modes, caps["supports_image"],
                f"{name} 的 image 模式与 supports_image 不一致")
            self.assertEqual(
                "multi" in modes, caps["supports_multi_image"],
                f"{name} 的 multi 模式与 supports_multi_image 不一致")


class TestFluxLocalRejects(unittest.TestCase):
    """2：flux_local 必须明确拒绝图生图（修复静默丢弃）。"""

    def test_declares_not_supported(self) -> None:
        p = make("flux_local")
        self.assertFalse(p.supports_image)
        self.assertFalse(p.supports_multi_image)
        self.assertEqual(p.max_references, 0)
        self.assertEqual(p.image_capabilities()["modes"], ["text"])

    def test_image_mode_error_rejects(self) -> None:
        p = make("flux_local")
        for mode in ("image", "multi"):
            err = p.image_mode_error(mode, 1)
            self.assertTrue(err, f"{mode} 模式应被拒绝")
            self.assertIn("不支持", err)

    def test_text_mode_passes(self) -> None:
        p = make("flux_local")
        self.assertEqual(p.image_mode_error("text", 0), "")

    def test_generate_raises_on_references(self) -> None:
        """真正调用 generate() 也要被拦住（不只是声明）。"""
        import asyncio

        from app.providers.base import GenerateRequest, ProviderError

        p = make("flux_local")
        req = GenerateRequest(
            prompt="x", size="1024x1024", n=1,
            extra={"image_mode": "image",
                   "reference_images": ["data:image/png;base64,AAA"]},
        )
        with self.assertRaises(ProviderError) as cm:
            asyncio.run(p.generate(req))
        self.assertEqual(cm.exception.code, "UNSUPPORTED_IMAGE_MODE")

    def test_guard_called_before_network(self) -> None:
        """校验必须在发起网络请求**之前**（否则白花一次付费调用）。

        flux_local 的 payload 里不含 image 字段，所以只要它抛了
        UNSUPPORTED_IMAGE_MODE，就说明在组包前就拦住了。
        """
        import asyncio

        from app.providers.base import GenerateRequest, ProviderError

        p = make("flux_local")
        req = GenerateRequest(
            prompt="x", size="2048x2048", n=1,   # 故意给非法尺寸
            extra={"image_mode": "multi",
                   "reference_images": ["a", "b"]},
        )
        with self.assertRaises(ProviderError) as cm:
            asyncio.run(p.generate(req))
        # 图生图错误应**优先于**尺寸校验抛出
        self.assertEqual(cm.exception.code, "UNSUPPORTED_IMAGE_MODE")


class TestImageModeError(unittest.TestCase):
    """3：image_mode_error 的判定分支。"""

    def test_text_always_ok(self) -> None:
        for name in ("qwen", "flux_local", "mock"):
            self.assertEqual(make(name).image_mode_error("text", 0), "")

    def test_missing_reference_rejected(self) -> None:
        """选了图生图却没给参考图 → 应报错。"""
        for name in ("qwen", "seedream", "gemini"):
            err = make(name).image_mode_error("image", 0)
            self.assertTrue(err, f"{name} 缺参考图时应报错")
            self.assertIn("至少", err)

    def test_multi_not_supported(self) -> None:
        """openai 声明不支持多图生图。"""
        p = make("openai")
        self.assertTrue(p.supports_image)
        self.assertFalse(p.supports_multi_image)
        err = p.image_mode_error("multi", 2)
        self.assertIn("不支持多图生图", err)

    def test_exceed_max_references(self) -> None:
        p = make("qwen")
        self.assertEqual(p.max_references, 3)
        self.assertEqual(p.image_mode_error("multi", 3), "")
        err = p.image_mode_error("multi", 4)
        self.assertIn("最多接受", err)

    def test_model_whitelist(self) -> None:
        """qwen 只有 3.0 系列支持图生图。"""
        p = make("qwen")
        p.model = "qwen-image-2.0"      # 不在白名单
        err = p.image_mode_error("image", 1)
        self.assertTrue(err)
        self.assertIn("qwen-image-3.0", err)

        p.model = "qwen-image-3.0"
        self.assertEqual(p.image_mode_error("image", 1), "")

    def test_unknown_mode_treated_as_image(self) -> None:
        """未知模式不应崩溃，按图生图对待。"""
        p = make("flux_local")
        self.assertTrue(p.image_mode_error("weird", 1))


class TestGuardsWired(unittest.TestCase):
    """5：各服务商 generate() 里确实接上了统一校验。"""

    def test_each_image_provider_calls_guard(self) -> None:
        providers_dir = ROOT / "app" / "providers"
        checkable = ["qwen.py", "openai.py", "gemini.py", "seedream.py", "flux_local.py"]
        missing = []
        for fname in checkable:
            text = (providers_dir / fname).read_text(encoding="utf-8")
            if "image_mode_error" not in text:
                missing.append(fname)
        self.assertFalse(
            missing,
            "以下服务商的 generate() 没有接入统一能力校验：" + ", ".join(missing))

    def test_custom_reuses_openai_adapter(self) -> None:
        """custom（自定义 OpenAI 兼容）应复用 OpenAI 适配器，从而支持图生图。"""
        self.assertIs(get_provider("custom"), get_provider("openai"))
        self.assertTrue(make("custom").supports_image)


if __name__ == "__main__":
    unittest.main(verbosity=2)
