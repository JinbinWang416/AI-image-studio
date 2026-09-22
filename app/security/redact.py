# -*- coding: utf-8 -*-
"""敏感字段脱敏（块 4-b，文档 §4 字段权限层）。

## 现状说明（重要）

本项目**当前的数据模型里没有联系电话字段** ——
`data/stores.json` 的门店数据只有标题、副标题、配色、行业等，
`output/` 的批次 manifest 只记录产出与来源标记。

因此本模块目前是**能力就绪、暂无生效对象**：
接入点已铺好，一旦将来往门店/客户数据里加联系方式，
只要在 `SENSITIVE_FIELDS` 里登记字段名，脱敏立即生效，无需改调用方。

## 设计

- **默认脱敏**：未持有对应权限者看到 `138****5678`
- **按权限放行**：持有 `customer.contact.read` 者看到完整值
- **递归处理**：字典 / 列表 / 嵌套结构都能处理
- **导出同样生效**：CSV/JSON 导出前必须过这一层（否则脱敏形同虚设）
"""
from __future__ import annotations

import re
from typing import Any

__all__ = [
    "SENSITIVE_FIELDS",
    "mask_phone",
    "mask_email",
    "mask_id_card",
    "redact_value",
    "redact_payload",
    "redact_csv_rows",
]


# 字段名 → 所需权限
SENSITIVE_FIELDS: dict[str, str] = {
    "phone": "customer.contact.read",
    "mobile": "customer.contact.read",
    "tel": "customer.contact.read",
    "telephone": "customer.contact.read",
    "contact": "customer.contact.read",
    "contact_phone": "customer.contact.read",
    "phone_number": "customer.contact.read",
    "手机号": "customer.contact.read",
    "联系电话": "customer.contact.read",
    # 邮箱与证件号同样属个人信息（文档 §4 字段权限）
    "email": "customer.contact.read",
    "邮箱": "customer.contact.read",
    "id_card": "customer.contact.read",
    "idcard": "customer.contact.read",
    "身份证": "customer.contact.read",
}

_PHONE_RE = re.compile(r"\b(\d{3})\d{4}(\d{4})\b")
_PHONE_DASH_RE = re.compile(r"\b(\d{3})\d{4}(\d{4})\b|\b(\d{3})[- ]\d{4}[- ](\d{4})\b")
_EMAIL_RE = re.compile(r"^([^@]{1,2})[^@]*(@.*)$")
_ID_RE = re.compile(r"^(\d{4})\d{8,12}(\w{0,4})$")


def mask_phone(value: str) -> str:
    """手机号脱敏：`13812345678` → `138****5678`。

    对非 11 位号码（如座机）保留前 3 后 4 的通用做法；
    长度不足 7 位则整体打码（避免"脱敏后反而能推断"）。
    """
    s = str(value or "").strip()
    if not s:
        return s
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return "*" * len(s)
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[7:]}"
    # 其它长度：保留前 3 后 4
    return f"{digits[:3]}{'*' * max(1, len(digits) - 7)}{digits[-4:]}"


def mask_email(value: str) -> str:
    """邮箱脱敏：`zhangsan@example.com` → `zh***@example.com`。"""
    s = str(value or "").strip()
    m = _EMAIL_RE.match(s)
    if not m:
        return "***"
    return f"{m.group(1)}***{m.group(2)}"


def mask_id_card(value: str) -> str:
    """证件号脱敏：保留前 4 后 2。"""
    s = str(value or "").strip()
    if len(s) < 8:
        return "*" * len(s)
    return f"{s[:4]}{'*' * (len(s) - 6)}{s[-2:]}"


def redact_value(field: str, value: Any,
                 field_map: dict[str, str] | None = None) -> Any:
    """按字段名脱敏单个值。

    Args:
        field_map: 自定义字段→权限映射；省略时用全局 ``SENSITIVE_FIELDS``。
            注意：早期版本这里硬编码全局表，导致自定义映射不生效（已修）。
    """
    fmap = field_map or SENSITIVE_FIELDS
    key = str(field or "").strip().lower()
    if key not in fmap and field not in fmap:
        return value
    if value is None:
        return value
    if "email" in key or "邮箱" in key:
        return mask_email(str(value))
    if "id" in key and ("card" in key or "身份证" in key):
        return mask_id_card(str(value))
    return mask_phone(str(value))


def redact_payload(data: Any, *, permissions: set[str] | frozenset[str],
                   field_map: dict[str, str] | None = None) -> Any:
    """递归脱敏。

    Args:
        data: 任意嵌套结构（dict / list / 标量）
        permissions: **调用方**持有的权限集合
        field_map: 自定义字段→权限映射（默认用 SENSITIVE_FIELDS）

    Returns:
        脱敏后的**新对象**（不修改入参）
    """
    fmap = field_map or SENSITIVE_FIELDS
    perms = set(permissions or ())

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                need = fmap.get(str(k)) or fmap.get(str(k).lower())
                # ⚠️ 容器优先：`contact` 这类名字既可能是"联系电话"字段，
                #    也可能是装着 phone/email 的对象。若把 dict 当标量脱敏，
                #    会把整个对象变成一串星号（早期版本的 bug）。
                if need and need not in perms and not isinstance(v, (dict, list, tuple)):
                    out[k] = redact_value(str(k), v, fmap)
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, (list, tuple)):
            return [walk(x) for x in node]
        return node

    return walk(data)


def redact_csv_rows(rows: list[dict], *, permissions: set[str] | frozenset[str],
                    columns: list[str] | None = None,
                    field_map: dict[str, str] | None = None) -> list[dict]:
    """CSV 导出前的脱敏。

    ⚠️ 导出是最容易泄漏个人信息的通道（文档 §7.8：导出需独立权限并留痕）。
    这里对每一行做与接口**完全相同**的脱敏，避免"界面看不到、导出却能拿到"。
    """
    fmap = field_map or SENSITIVE_FIELDS
    perms = set(permissions or ())
    cols = columns or (list(rows[0].keys()) if rows else [])

    out: list[dict] = []
    for row in rows:
        item: dict = {}
        for col in cols:
            value = row.get(col)
            need = fmap.get(str(col)) or fmap.get(str(col).lower())
            if need and need not in perms and not isinstance(value, (dict, list, tuple)):
                item[col] = redact_value(str(col), value, fmap)
            else:
                item[col] = value
        out.append(item)
    return out


def is_redacted(value: Any) -> bool:
    """判断一个值是否已被脱敏（用于测试与自检）。"""
    return isinstance(value, str) and "****" in value
