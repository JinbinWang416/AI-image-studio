# -*- coding: utf-8 -*-
"""兼容转发：实现在 `app/effect/renderer.py`（Phase 1 分包后）。

保留本模块是为了让 `from .effect_renderer import X` 这类既有写法继续可用。
新代码请直接 `from ..effect.renderer import X`。
"""
from .effect.renderer import *  # noqa: F401,F403
