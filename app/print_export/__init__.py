# -*- coding: utf-8 -*-
"""印刷 TIF 自动导出。

把 AI 生成的 RGB PNG（屏幕稿）处理成印刷厂可直接 RIP 的多图层 TIF：

```
output/<批次>/<门店>/
├── _work/          母版区（硬链接指向原 PNG，不占额外空间）
├── 印刷TIF/        对外交付
│   ├── <门店>_CMYK.tif        彩色层（印刷主版）
│   ├── <门店>_白墨.tif         白墨层（玻璃静电贴必印）
│   ├── <门店>_刀模.tif         刀模线层（模切用，不印色）
│   ├── <门店>_合并预览.tif     三层合并，人工核对
│   └── print_manifest.json    全部印刷参数
├── 预览/           前端展示用 JPEG（浏览器打不开 TIF）
├── <门店>生成图/   原 PNG **原地保留**
├── <门店>效果图/   保留不动
└── _manifest.json  追加 print_export 字段
```

## 快速使用

```python
from app.print_export import PrintExporter, export_after_batch

# 单店
exporter = PrintExporter(cfg)                 # 批量时复用实例（缓存 rembg 会话）
result = exporter.export_store(store_dir, source_png, store_name="01_房屋中介门店")

# 批次（orchestrator 用；绝不抛异常）
results = export_after_batch(cfg, [(store_dir, source_png, name), ...])
```

## 已知限制

- **分辨率**：60cm @ 300DPI 需要 7087px，而 AI 源图通常 1024px。
  插值不会增加真实细节，manifest 里会同时记录 `dpi` 与 `effective_dpi`。
- **CMYK 校色**：偏色保护只覆盖白底/亮蓝/大红三类，其余颜色仍需人工校。
- **ICC**：优先用 `config/icc/` 里的 profile；缺失时回落系统 `RSWOP.icm`，
  再缺则朴素转换并记警告。
- **rembg**：可选依赖。未安装时自动降级为纯色去背（适合背景纯净的 AI 图）。
"""

from .manifest import (
    PRINT_DIR_NAME,
    PRINT_VERSION,
    PREVIEW_DIR_NAME,
    WORK_DIR_NAME,
    ErrorCode,
    PrintManifest,
    read_print_manifest,
    update_batch_manifest,
    write_print_manifest,
)
from .print_export import (
    PrintExportResult,
    PrintExporter,
    collect_export_targets,
    export_after_batch,
    write_batch_status,
)

__all__ = [
    "PRINT_VERSION",
    "PRINT_DIR_NAME",
    "WORK_DIR_NAME",
    "PREVIEW_DIR_NAME",
    "ErrorCode",
    "PrintManifest",
    "PrintExporter",
    "PrintExportResult",
    "export_after_batch",
    "collect_export_targets",
    "write_batch_status",
    "write_print_manifest",
    "read_print_manifest",
    "update_batch_manifest",
]
