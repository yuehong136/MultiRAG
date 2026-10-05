# Embedding 批次累积

当前生产注册表使用 `core/llm/embedding.py`，其中 `BuiltinEmbed` 包装内建 TEI，
`DefaultEmbedding` 对应 BAAI。本地 `embedding_model/default_embedding.py` 的兼容
工厂入口重导出同一实现。进程内模型加载及 FastEmbed 退役边界见
[进程内 embedding](in-process-embedding.md)。

Builtin/default、tokenizer、普通任务及 dataflow 均先收集批次，最后沿 axis 0 合并
一次。单批次直接返回原数组，保留 dtype、shape 和顺序，token 统计与截断规则不变。
Builtin/default 的空列表仍返回 `(None, 0)`；tokenizer/dataflow 保留空输出短路。
普通任务补齐空 docs 返回 0，避免访问不存在的标题向量。

Tokenizer 继续使用原来的一维标题展开语义。将它直接改成二维 tile 会改变标题权重
是否生效，超出本次纯累积优化；本次只用等价的一维 tile 替代标题 concatenate。
普通任务现有的二维标题权重混合保持不变。

`tests/unit/test_embedding_batch_accumulation.py` 覆盖批次边界、尾批、输入顺序、
float32/float64、向量维度、空输入、token 及送入索引的数据。集成测试
`test_embedding_batches_preserve_stored_vectors_and_token_ledgers` 使用受控模型输出，
运行普通 worker 和 dataflow，再独立读回 Milvus 向量和 PostgreSQL 计数。

复现拷贝量与本机计时：

```bash
uv run --no-sync python -m tests.manual.benchmark_embedding_accumulation
```

脚本调用实际 `BuiltinEmbed.encode`，与旧累积循环比较；相同的 float32、768 维、
每批 16 行数据逐元素相等，shape、dtype 和 token 相同。以下是一次本地运行的
累积拷贝字节数（不包含模型推理）：

| 批次数 | 旧循环 | 单次合并 | 旧/新 |
| --- | ---: | ---: | ---: |
| 8 | 1,720,320 | 393,216 | 4.375 |
| 32 | 25,903,104 | 1,572,864 | 16.469 |
| 128 | 405,749,760 | 6,291,456 | 64.492 |

累积拷贝从 O(B²) 降到 O(B)，B 为等大小批次数。峰值内存仍为 O(N)：批次数组与
合并结果会同时存活，并非零额外内存。计时受机器负载影响，不能推导真实服务或
端到端索引吞吐量；本次未进行真实 embedding 服务性能验证。

已知存储限制：dataflow 当前仅赋值 `q_<dim>_vec`，Milvus schema 转换会将缺失的
标准 `vector` 补零。本次使用优化前代码重现了相同结果；集成测试分别核对真实
`q_768_vec`、标准字段和 token 账本，未将此历史字段映射问题算作修复。
