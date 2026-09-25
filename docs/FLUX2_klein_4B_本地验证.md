# FLUX.2 klein 4B 本地样图验证

本实现使用 Black Forest Labs 官方 `flux2` 推理核心，模型固定为 `flux.2-klein-4b` 的四步蒸馏版本。官方源码、权重和环境均位于 `E:\AIModels\flux2-klein-4b`；运行时服务为 `app/local_flux_service.py`，只绑定 `127.0.0.1:8189`。

服务启动时将 Qwen3 FP8 文本编码器、FLUX Flow 模型和 VAE 保留在 CPU 内存。每一阶段只把当前所需组件放到 RTX 4070 Laptop 的 GPU，因此不会让三者同时占用 8GB 显存。服务不经过官方交互式 CLI，以避免其可选的提示词扩写和图生图路径。

接口如下：

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 返回模型状态、GPU、驱动、Torch/CUDA 版本、显存和本地模型清单摘要。模型缺失或加载失败返回 503 和可操作原因。 |
| `POST /generate` | 只接收 `prompt`、可选 `seed` 和固定 `width=height=1024`；返回 PNG Base64、种子、耗时与峰值显存。额外字段、负向词和参考图字段都会被拒绝。 |

Web 层的 `flux_local` 服务商只允许调用专用的本地验证入口。该入口固定门店 `01`、V8、6 张、单并发，强制背景刷白，输出根目录为 `output_local_validation`。通用「开始生成」、单图重生成和失败重试均会拒绝该服务商，防止 138 张量产误用本地模型。

运行结束后会生成：

- `output_local_validation/01_房屋中介门店/_manifest.json`：每张图片的服务商、模型、种子、耗时、峰值显存和状态。
- `output_local_validation/_local_validation_report.json`：6 张 PNG 的尺寸、边缘纯白比例、SHA-256 和技术结论。
- `output_local_validation/_人工评分表.csv`：逐张填写主/副标题准确性、行业主体、构图、白底和可制作性。

评级规则：技术链路不完整、OOM、非 PNG 或边缘非纯白为“**不通过**”；技术链路完整但文字/审美不达标为“**实验可用**”；六张主副标题均准确且平均构图评分至少 `4/5` 为“**量产候选**”。
