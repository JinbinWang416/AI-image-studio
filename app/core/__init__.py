# -*- coding: utf-8 -*-
"""核心层（Phase 0 引入）。

按架构复盘文档第 2 节的包结构，`core/` 放跨模块的基础设施：
配置、模型、事件、异常。

**当前只迁入了配置**（Phase 0）。其余在后续 Phase 按需迁入，
每一步都可独立验证、可回滚。
"""
from __future__ import annotations

__all__ = ["config"]
