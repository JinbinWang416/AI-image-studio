# 把印刷厂的 CMYK ICC profile 放这里

印刷导出的 CMYK 转换（`app/print_export/cmyk.py` 的 `find_icc_profile()`）
按这个顺序查找 profile：

1. 设置里**显式指定**的路径
2. **本目录** —— 放任意 `*.icc` / `*.icm`，按文件名排序取第一个
3. 系统颜色目录 `C:\Windows\System32\spool\drivers\color`，文件名须为下列之一：
   `RSWOP.icm`、`USWebCoatedSWOP.icc`、`ISOcoated_v2_eci.icc`、
   `CoatedFOGRA39.icc`、`default_cmyk.icc`

三处都没有时，导出**仍会成功**，但会降级为朴素数学转换（**颜色会有偏差**），
并在 `print_manifest.json` 的 `icc.note` 里写入警告。

## 去哪拿

- **优先向印刷厂索取** —— 只有匹配他们实际印刷条件（机器、油墨、纸张）的
  profile 才准，网上随便下的不一定对
- 或从 Adobe 安装目录复制，例如 `C:\Program Files\Adobe\...\Profiles\`
- 系统里已装的，可从 `C:\Windows\System32\spool\drivers\color` 复制一份过来

## ⚠️ 不要提交到仓库

ICC profile 通常有版权，且只对本机印刷条件有意义。
本目录下的 `*.icc` / `*.icm` 已被 `.gitignore` 排除，只保留这份说明。
