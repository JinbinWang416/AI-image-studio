# -*- coding: utf-8 -*-
"""敏感字段脱敏测试（块 4-b）。"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.redact import (  # noqa: E402
    mask_email,
    mask_id_card,
    mask_phone,
    redact_csv_rows,
    redact_payload,
    redact_value,
)

FULL = "customer.contact.read"


class MaskTests(unittest.TestCase):
    def test_phone_11_digits(self) -> None:
        self.assertEqual(mask_phone("13812345678"), "138****5678")
        self.assertEqual(mask_phone("138-1234-5678"), "138****5678")
        self.assertEqual(mask_phone("138 1234 5678"), "138****5678")

    def test_phone_landline(self) -> None:
        """座机号同样脱敏（保留前 3 后 4）。"""
        self.assertIn("*", mask_phone("01012345678"))

    def test_phone_too_short_fully_masked(self) -> None:
        """位数太少时整体打码 —— 否则脱敏后仍可推断原号。"""
        self.assertEqual(mask_phone("12345"), "*****")

    def test_phone_empty(self) -> None:
        self.assertEqual(mask_phone(""), "")
        self.assertEqual(mask_phone(None), "")

    def test_email(self) -> None:
        self.assertEqual(mask_email("zhangsan@example.com"), "zh***@example.com")
        self.assertEqual(mask_email("不是邮箱"), "***")

    def test_id_card(self) -> None:
        out = mask_id_card("110101199001011234")
        self.assertTrue(out.startswith("1101"))
        self.assertIn("*", out)


class RedactPayloadTests(unittest.TestCase):
    DATA = {
        "store": "房屋中介",
        "main_title": "房屋中介",
        "phone": "13812345678",
        "contact": {"phone": "13900001111", "email": "a@b.com", "note": "可联系"},
        "items": [{"phone": "13700002222", "name": "x"}],
    }

    def test_without_permission_masks(self) -> None:
        out = redact_payload(self.DATA, permissions=set())
        self.assertEqual(out["phone"], "138****5678")
        self.assertEqual(out["contact"]["phone"], "139****1111")
        self.assertIn("***", out["contact"]["email"])
        self.assertEqual(out["items"][0]["phone"], "137****2222")
        # 非敏感字段不受影响
        self.assertEqual(out["main_title"], "房屋中介")
        self.assertEqual(out["contact"]["note"], "可联系")

    def test_with_permission_passes_through(self) -> None:
        out = redact_payload(self.DATA, permissions={FULL})
        self.assertEqual(out["phone"], "13812345678")
        self.assertEqual(out["contact"]["phone"], "13900001111")
        self.assertEqual(out["contact"]["email"], "a@b.com")

    def test_does_not_mutate_input(self) -> None:
        before = str(self.DATA)
        redact_payload(self.DATA, permissions=set())
        self.assertEqual(str(self.DATA), before, "脱敏不应修改入参")

    def test_nested_lists(self) -> None:
        data = [[{"phone": "13800000000"}]]
        out = redact_payload(data, permissions=set())
        self.assertEqual(out[0][0]["phone"], "138****0000")

    def test_field_alias(self) -> None:
        """`mobile` / `tel` / 中文名 都要能识别。"""
        for key in ("mobile", "tel", "telephone", "contact_phone", "手机号", "联系电话"):
            out = redact_payload({key: "13812345678"}, permissions=set())
            self.assertEqual(out[key], "138****5678", f"{key} 未被脱敏")

    def test_custom_field_map(self) -> None:
        out = redact_payload({"secret_contact": "13812345678"}, permissions=set(),
                             field_map={"secret_contact": "customer.contact.read"})
        self.assertEqual(out["secret_contact"], "138****5678")

    def test_null_and_nonstring(self) -> None:
        out = redact_payload({"phone": None, "email": 12345}, permissions=set())
        self.assertIsNone(out["phone"])
        self.assertIn("*", str(out["email"]))


class RedactCsvTests(unittest.TestCase):
    def test_csv_masked_without_permission(self) -> None:
        rows = [{"store": "A", "phone": "13812345678"},
                {"store": "B", "phone": "13900001111"}]
        out = redact_csv_rows(rows, permissions=set())
        self.assertEqual(out[0]["phone"], "138****5678")
        self.assertEqual(out[1]["phone"], "139****1111")
        self.assertEqual(out[0]["store"], "A")

    def test_csv_full_with_permission(self) -> None:
        rows = [{"phone": "13812345678"}]
        out = redact_csv_rows(rows, permissions={FULL})
        self.assertEqual(out[0]["phone"], "13812345678")

    def test_csv_columns_subset(self) -> None:
        rows = [{"a": 1, "phone": "13812345678"}]
        out = redact_csv_rows(rows, permissions=set(), columns=["a"])
        self.assertNotIn("phone", out[0])


class RedactValueTests(unittest.TestCase):
    def test_unknown_field_passthrough(self) -> None:
        self.assertEqual(redact_value("store", "房屋中介"), "房屋中介")

    def test_known_field_masked(self) -> None:
        self.assertEqual(redact_value("phone", "13812345678"), "138****5678")


if __name__ == "__main__":
    unittest.main(verbosity=2)
