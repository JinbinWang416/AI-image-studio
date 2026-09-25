# -*- coding: utf-8 -*-
"""兼容转发：实现在 `app/validation/local.py`（Phase 1 分包后）。

保留本模块是为了让 `from .local_validation import X` 这类既有写法继续可用。
新代码请直接 `from ..validation.local import X`。
"""
from .validation.local import *  # noqa: F401,F403
