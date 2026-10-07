# GraphRAG 暂停、恢复与存储边界

GraphRAG 的正式 worker 调用 `core/graphrag/general/index.py:run_graphrag_for_kb`。
完成本轮文档的合并后，resolution 和 community 可以分别从成功持久化的阶段恢复。

## API 与 Web

- `DELETE /api/v1/datasets/{id}/index?type=graph&wipe=false` 请求暂停，保留 Task、
  dataset task ID、graph/subgraph/entity/relation/community_report 与阶段 marker。
  graph/raptor/mindmap 三个显式 DELETE 路径也接受 `wipe`；省略仍为 true。
- Web GraphRAG 暂停携带 `task_id=<当前 trace ID>`。绑定已经变化时拒绝请求，不能取消新任务。
  RAPTOR 的 Web 暂停继续使用原 Task cancel API；显式删除保持默认 wipe。
- 暂停响应只确认已提交取消信号。Task progress 记为 -1，保留 trace；运行中的 writer 尚未
  收束时，重新启动会返回业务失败。当前 Web 将负进度展示为 Failed，并通过 Retry 重启；
  暂停成功提示明确说明等待任务响应。它不是对 executor 已退出的确认。
- 默认 wipe 与 `DELETE /datasets/{id}/knowledge_graph` 清理上述五类图产物及两个 marker，
  取消并删除当前 Task、清空图任务绑定与完成时间，保留普通源 chunks 与操作日志。
  Redis 取消失败、快照读取失败、writer 等待超时或删除读回失败均返回失败。
- Web 详情既有 DTO adapter 将 `document_count/chunk_count` 映射为 `doc_num/chunk_num`；
  不增加旧协议别名。生成按钮继续消费该 adapter，显式 0 保留。

## 成功 checkpoint 的含义

marker 使用 `graphrag:phase:{kb_id}:{resolution_done|community_done}`，TTL 为七天。
值是持久化图内容与 GraphRAG 配置的摘要，而不是无条件的 `1`。
旧 marker、版本不同、Redis 查询/设置失败都只产生 cache miss。
新内容合并前清理 marker；图 checkpoint 的新 revision 也使旧值失效，因此 Redis 清理失败
不会令新图跳过阶段。wipe 后没有图可恢复；下一张图有新的 revision。

恢复先查完整持久化图：已合并的文档不再抽取或重复合并；未合并的文档可复用 subgraph。
全局图在文档处理前读取一次；subgraph 查询在存储端按文档过滤，避免恢复时反复扫描全库。
没有新文档、甚至没有可用源 chunks 时，已有图仍可以完成待处理阶段。
纯恢复的 resolution 使用全部图节点作为候选 anchor。损坏或多份歧义 checkpoint 明确失败，
不能当作空图重新生成并覆盖。文档重解析/删除仍依赖已有 source 生命周期使图失效；本轮
新增内容验收针对新增 source document，不承诺忽略生命周期更新直接覆盖同一文档的源行。

resolution 的写入和清理全部完成后才设置 marker。community 的枚举、写入、读回与 stale
清理全部成功后才设置 marker。模型超时、异常或无效 community JSON/结构会传播失败。
marker 存储失败允许本轮已完成的阶段返回，但下一次会重新执行。

## 写入与并发

当前安全路径使用 Milvus 的原子 key upsert、Strong 读回与完整 query iterator；
不使用会吞查询/删除异常的通用 search/delete。源 chunks 也使用 Strong 完整读取，避免
刚写入新内容时读到旧快照而跳过重建。超过存储 schema 字节上限的 checkpoint 先拒绝，
不能截断 JSON 后把阶段标为成功。

所有 embeddings 和行先构造、校验，再写辅助行，最后 upsert/readback 权威 graph checkpoint；
之后只删除完整快照中的旧 ID。graph、per-doc subgraph、entity 与 relation 使用稳定 key。
community key 按 `(kb_id, title)` 稳定生成；重复 title 拒绝，避免覆盖另一个报告。
清理依据当前图以及稳定 ID，可在上次 prune 失败后再次清理残留的旧 entity/relation。

`GRAPHRAG_INSERT_BULK_SIZE` 默认 64，`GRAPHRAG_INSERT_CONCURRENCY` 默认 4，均至少为 1。
每次调用只有固定数量的 writer workers，每批最多重试三次；普通写入最多 256 行在途。
失败或 coroutine 取消时停止派发、取消并等待所有 workers，已经进入 executor 的同步存储 IO
必须真正结束后才能返回。超时也先收束底层调用再重试，不启动重叠写入。同步 RPC 不能被 coroutine 强制终止，
停止延迟取决于驱动调用何时返回；等待期间继续持有 writer lease。

唯一 token 的 `graphrag:writer:{tenant_id}:{kb_id}` lease 覆盖整个正式 GraphRAG 调用和 wipe，
默认 1200 秒、每三分之一 TTL 续约；写入前检查所属权，失去 lease 时失败。
另用 `graphrag:control:{kb_id}` 串行化 API 排队、暂停及 wipe 的绑定变化。
API 最多等待 30 秒，未取得 lease 返回失败；不会先删 checkpoint。
resolution 的 networkx 合并继续使用原有互斥锁，LLM 判定仍可并发。

这不是跨多行或跨 Redis/SQL/Milvus 的事务。失败的辅助行写入可以留下部分新行；同 ID 的
community 报告可能已有部分更新。旧权威 graph 不会通过 delete-then-insert 消失，marker
不会宣称未完成阶段成功，后续重跑按稳定 key 覆盖并清理。

## 生产边界

- 所有 API/worker 实例必须升级并使用相同 Redis 与新 lease。旧 worker、直接写存储、
  其他不参加 lease 的文档生命周期写入不在互斥保证内；发布前应停止旧 GraphRAG writer。
- lease 无 Milvus 服务端 fencing。长时间进程停顿、网络分区或远端在客户端超时后继续
  提交，不能证明严格线性一致；发现 lease 丢失会停止后续写入，仍须排查在途 RPC。
- 64×4 是每次调用的上限；跨数据集、进程或机器会累加。调参需结合连接池与模型容量。
- checked checkpoint 当前只允许 Milvus；Infinity 的 insert 会先删除同 key，其他后端也
  尚未提供本合同的 checked scan/upsert/readback，明确拒绝，不回退为破坏性替换。
- 图 JSON 受当前 Milvus schema 的 VARCHAR 长度限制；本轮没有新增大图分片或 schema 迁移。
- 验收为自有 SQL 库、Milvus 集合、Redis keys 与 HTTP listener；提取器与模型输出受控，
  正式 GraphRAG 编排、resolution、Leiden/community、存储及 Web 请求实际执行。
  没有重启常驻服务、使用商业模型或验证生产分区/多机压力与真实 worker 进程崩溃恢复。

## 复现

```bash
make verify
MULTIRAG_GRAPHRAG_EVIDENCE_DIR="$PWD/.test-results/graphrag-checkpoints" \
  make integration INTEGRATION_WORKERS=1 \
  TESTS='tests/integration/test_graphrag_checkpoint_lifecycle.py tests/integration/test_dataset_index_http.py'
MULTIRAG_ACCEPTANCE_WEB_CHECKOUT=/absolute/path/to/web \
  MULTIRAG_GRAPHRAG_EVIDENCE_DIR="$PWD/.test-results/graphrag-checkpoints" \
  make integration INTEGRATION_SUITE=acceptance INTEGRATION_WORKERS=1 \
  TESTS=tests/acceptance/test_graphrag_web.py
```

验收保留每个随机 dataset 的原生行、SQL 任务/绑定、marker 值及清理断言；Web 另保留请求
读回与六张明暗主题截图、带文件名/路由/视口的 contact sheet。外部字体 CDN 在浏览器
验收中使用空 CSS 资产替身，页面使用本地 fallback 字体，业务响应不伪造。
