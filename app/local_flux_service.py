# -*- coding: utf-8 -*-
"""兼容转发：实现在 `app/local_service/flux.py`（Phase 1 分包后）。

保留本模块是为了让 `from .local_flux_service import X` 这类既有写法继续可用。
新代码请直接 `from ..local_service.flux import X`。
"""
from .local_service.flux import *  # noqa: F401,F403
