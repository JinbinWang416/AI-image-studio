# -*- coding: utf-8 -*-
"""兼容转发：实现在 `app/state/manifest_store.py`（Phase 1 分包后）。

保留本模块是为了让 `from .manifest import X` 这类既有写法继续可用。
新代码请直接 `from ..state.manifest_store import X`。
"""
from .state.manifest_store import *  # noqa: F401,F403
