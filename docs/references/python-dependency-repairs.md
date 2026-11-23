# Python 依赖兼容约束

- FastMCP、FastMCP Slim 和 MCP SDK 随项目锁文件升级。旧协议的进程端仍使用自己的锁文件和包源。运行 `make mcp-compat` 和 Agent 绑定检查可验证两个协议版本上的真实 HTTP 会话。
- BCEmbedding 0.1.5 的依赖声明限制 Transformers 低于 4.37。对该版本单独指定已验证的 Transformers 5.23.0，保留该模型的加载、编码和重排接口。模型加载回归使用合成权重覆盖 safetensors 和 PyTorch 权重格式。
- Infinity SDK 固定为 0.7.0。该版本的源码未使用旧版 Setuptools 的资源接口，因此保留 SDK 和协议版本，约束 Setuptools 到已修复的 84 系列。DOCX 子文档合并通过 docxcompose 1.4.1 使用标准库资源接口。XGBoost 1.7.6 保留现有 DeepDoc 模型加载入口。
- Accelerate 使用公开的 [PR #4214 固定提交](https://github.com/huggingface/accelerate/pull/4214/commits/a35af225361fe0097900680e850c84bae97340b7)。正式发布版提供等效修复后，重新锁定发布版并验证本地分片和 Hub 缓存的加载行为。
