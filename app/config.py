# -*- coding: utf-8 -*-
"""配置（**门面模块**）。

> Phase 0 架构重构后，真正的实现在 `app/core/config.py`。
> 本文件保留为**转发门面**，目的是让所有既有 import 一字不改：
>
>     from .config import Config           # 继续可用
>     from ..config import DATA_FILE       # 继续可用
>     from app.config import load_config   # 继续可用
>
> 这样做的好处是重构**完全可逆** —— 想回退只需恢复本文件内容。

**优先级：环境变量 > `config/settings.json` > 内置预设**

- `.env` / 环境变量：仅用于本机临时覆盖与密钥（优先级最高）
- `settings.json`：Web 设置界面读写，改完即时生效，无需重启
- 内置预设：`app/providers/catalog.py` 里的默认值

## 配置结构（Phase 0 后）

`Config` 由 12 个按关注点分组的子配置组合而成，
旧有的 57 个平铺字段名通过 `@property` **继续可用**（读、写都行）：

```python
cfg.print.dpi          # 新写法（子配置）
cfg.print_dpi          # 旧写法（兼容属性）—— 两者等价
```

需要用旧字段名做「覆盖式复制」时，用 `with_config()` 代替 `dataclasses.replace()`：

```python
from app.config import with_config
cfg2 = with_config(cfg, output_root=some_path, budget_limit=18)
```
"""
from __future__ import annotations

# ---------------------------------------------------------------- 常量
from .core.config import (
    CUTOUT_MODES,
    DATA_FILE,
    ENV_FILE,
    FONT_DIR,
    LOCAL_PROFESSIONAL_ROOT,
    LOCAL_VALIDATION_ROOT,
    LOG_DIR,
    OUTPUT_ROOT,
    PROVIDER_PRESETS,
    ROOT,
)

# ---------------------------------------------------------------- 子配置
from .core.config import (
    EffectConfig,
    GenerationConfig,
    GuardConfig,
    ImageWorkflowConfig,
    OptimizerConfig,
    OutputConfig,
    PostprocessConfig,
    PrintConfig,
    PromptConfig,
    ProviderConfig,
    QcConfig,
    ScopeConfig,
)

# ---------------------------------------------------------------- 主配置与工具
from .core.config import (
    FLAT_TO_GROUP,
    Config,
    load_config,
    load_dotenv,
    make_config,
    with_config,
)

__all__ = [
    # 路径常量
    "ROOT",
    "ENV_FILE",
    "DATA_FILE",
    "OUTPUT_ROOT",
    "LOCAL_VALIDATION_ROOT",
    "LOCAL_PROFESSIONAL_ROOT",
    "LOG_DIR",
    "FONT_DIR",
    "CUTOUT_MODES",
    "PROVIDER_PRESETS",
    # 子配置
    "ProviderConfig",
    "GenerationConfig",
    "OutputConfig",
    "QcConfig",
    "PromptConfig",
    "OptimizerConfig",
    "GuardConfig",
    "PostprocessConfig",
    "EffectConfig",
    "PrintConfig",
    "ScopeConfig",
    "ImageWorkflowConfig",
    # 主配置与工具
    "Config",
    "FLAT_TO_GROUP",
    "load_config",
    "load_dotenv",
    "with_config",
    "make_config",
]
