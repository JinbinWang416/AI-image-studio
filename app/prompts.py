# -*- coding: utf-8 -*-
"""兼容转发：实现在 `app/prompt/templates.py`（Phase 1 分包后）。

保留本模块是为了让 `from .prompts import X` 这类既有写法继续可用。
新代码请直接 `from ..prompt.templates import X`。
"""
from .prompt.templates import *  # noqa: F401,F403
