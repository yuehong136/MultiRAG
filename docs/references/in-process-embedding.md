# 进程内 embedding

Python 后端的 BAAI 供应商由 `core/llm/embedding.py` 中的 `DefaultEmbedding`
实现，使用 FlagEmbedding / PyTorch 在本进程加载模型，不需要 TEI 服务。
`Builtin` 则继续使用已配置的 TEI；两者是独立运行方式。

加载器按请求的模型名下载并缓存权重，已有 `~/.ragdatav/<模型短名>` 缓存仍可复用。
调用方也可通过 `model_path` 指定完整本地模型目录；路径缺失或权重损坏直接失败，
不会回退成其他模型。`local_files_only=True` 限制 HuggingFace 查找本地缓存。
模型加载不再改写进程的 `CUDA_VISIBLE_DEVICES`；FlagModel 自行选择 CUDA、MPS、
NPU 或 CPU，CUDA 可用时沿用 FP16。多个实例保留各自模型引用，不会因后续加载
另一模型而串用权重。

文档仍每批 16 条编码、合并一次，保持输入顺序、原有截断和 token 统计；空输入
返回 `(None, 0)`。查询返回完整的一维向量。默认使用原有中文查询指令、CLS 池化
和归一化；特殊模型可通过内部构造参数显式指定 `query_instruction`。

## FastEmbed 退役与模型迁移

`fastembed` / `fastembed-gpu` 依赖及实现已退出。供应商目录不再提供 FastEmbed；
已有数据库中的旧供应商条目也会从可添加列表过滤。读取已有 FastEmbed 租户模型
会明确报迁移错误，不自动改配置、模型名或已有向量。

需要迁移的环境应先清点租户模型、租户默认模型、知识库和其他 embedding 引用，
再确认实际模型可由 FlagModel 加载。添加对应 BAAI 模型后验证文档/查询向量、
维度、归一化、池化、查询指令和检索相关性；建立新索引后再切换引用。
不能仅凭模型名称或维度相同复用 FastEmbed 的量化 ONNX 向量。
本实现不承诺 FlagModel 支持 FastEmbed 曾支持的全部架构。

中文质量评测使用固定 revision 和 SHA256 校验的 `BAAI/bge-small-zh-v1.5`
Safetensors 权重，调用同一个生产 BAAI 驱动，并显式使用空查询指令。
评测会新建并清理独立索引，不复用旧 ONNX 评测向量：

```sh
make eval-assets
make eval
```

Pillow 是生产直接依赖，下限为 12.2.0、上限为 13；dev/test 复用同一依赖。
FastEmbed 退出后锁文件保留 NumPy 1.26.4 和 Infinity SDK 0.7.0.dev5，
不覆盖它们及 graspologic 的 NumPy 上界，也不要求更换 Infinity 服务端。
