# 多模型部署说明

本部署包不包含任何 API Key、模型权重、输出图片或历史运行记录。解压到目标 Windows 机器后，先安装 Python 3.12，然后在包根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\bootstrap_deploy.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\local-server.ps1 -NoBrowser
```

打开 `http://127.0.0.1:8000`，在“设置”填入目标服务商的 Key；密钥仅保存在该机器的 `config\settings.json`，不应复制、提交或放回部署包。

| 路线 | 部署内容 | 使用方式 |
| --- | --- | --- |
| OpenAI GPT Image | 已内置适配器 | 填写 `OPENAI_API_KEY` 或网页设置的 OpenAI Key；选择后在门店 01 运行“生成 6 张新图”。 |
| DeepSeek | 已内置提示词优化器 | 填写 `DEEPSEEK_API_KEY` 或网页设置的 DeepSeek Key；只在 OpenAI 房屋中介 6 图流程中优化提示词。 |
| 阿里云百炼 Qwen Image | 已内置适配器 | 填写 `DASHSCOPE_API_KEY`，选模型后按常规生成流程运行。 |
| FLUX.2 klein 4B | 附安装和启动脚本，权重不随包分发 | 先运行 `tools\setup_flux_local.ps1` 下载并校验模型，再启动 `tools\start_flux_local.ps1`。 |
| 可灵、智谱 | 适配器代码与设置项已保留 | 需要相应平台 Key 和可用的图像模型权限；先在网页“测试连接”。 |

## 发布包校验

`RELEASE-MANIFEST.json` 记录包内文件的 SHA-256、版本和模型范围。它声明 `secrets_included=false`、`model_weights_included=false`。部署前可审阅该清单；解压后运行 `main.py check` 会验证 23 套门店与 138 条提示词。

## OpenAI 房屋中介再生成

该流程固定使用 1024×1024 PNG、质量优先、单并发，只生成门店 `01` 的 6 张。每次运行会保存新变体提示词、感知哈希、耗时和历史版本；停止操作会取消当前等待的请求并阻止后续主题发起。

## 本地网络边界

网页服务仅监听 `127.0.0.1:8000`，FLUX 服务仅监听 `127.0.0.1:8189`。云端调用只会在用户自行填入并选择对应 API 服务商后发生。
