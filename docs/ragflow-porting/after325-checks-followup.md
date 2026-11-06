# after325 遗留检查收尾

2026-11-06，从 `9f0a60e202b97631d861115f0f81032ca915e8ca` 收尾
[本批交付](batch-after-32559d2d.md)保留的一项 harness 失败和五条 mypy 错误。
原始交付、失败及 `strictProtocolPassed=false` 的历史记录保留。

## 修复与合同

`test_search_detail_guard.py` 只验证 PostgreSQL SearchService，没有外部消费者调用。
将其移出 consumer 清单，保留在 database/core 套件；完整消费者 marker 清单一致性断言保持。
回归同时确认它继续归属 PostgreSQL 验证，未用添加虚假 marker 或放宽断言处理。

文档历史刷新显式检查三个分片计数都是严格整数后再比较；缺失、bool、字符串和 float
仍失败关闭，未分配 replica 仍允许。ES 信息字典补类型声明；SQL 匹配对象与替换文本分别命名，
保留原来的 tokenized LIKE/等号重写和多条件处理。

对照上游 `3f929e3904ad49476071cfd5eae02089cdc7649a`、
`de18313f97ffe5f008e98e8188a4d7500ca2dae7`、
`2971849783b879749ae25732f202e42acf0a3943`：无效生成 SQL 继续 warning 并传播错误，
连接故障仍保留 traceback；parse/stop 的严格缺索引与删除恢复边界不变。
本次是对应问题链的本地分类/类型修复，不声明上游也有这些类型错误。
上游参考上限保持 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，没有 fetch 或扩批。

## 定向验证

- 修复前复现 harness 的 46 通过、1 失败，以及两个文件的全部五条 mypy 错误。
- 修复后组合单测 136 项通过，零失败、零 skip；包含完整 harness、文档历史、缺索引和 ES SQL 回归。
- 两个目标文件的 mypy、六个改动 Python 文件的 Ruff check/format 通过。
- PostgreSQL 实际搜索详情与失败传播 6 项通过，使用隔离 scratch 库，收集前准备服务，`REQUIRE_SERVICES=1`。
- 独占 Elasticsearch 8.11.3 容器运行现有原生历史删除/恢复/可见性用例，验证 sibling/外数据集保留、重复删除和清理。

运行使用自有 Python 环境，借用基础依赖只读，单独安装锁定 LiteLLM `1.82.5`，未修改共享 `.venv`。
源码、锁、配置和消费者 pin 的输入哈希、命令原始 exit/JUnit、首败及精确资源清理读回保存在本地收据。
本轮曾有自有 venv 的 macOS 动态库准备失败，以及只读清理 verifier 的 helper 参数错误；修正后分别复跑，原始失败保留。

Web master/pin 为 `3e473d614e70e9ccdd57c4ea4c715833effbd6e2`；没有消费者合同变化，无需更新。
这组局部检查不替代全库门禁或远程 `ci-required`；未 push、未部署，真实供应商及生产未验收。
完整 Go 本地 parse/chunk/EXPLAIN 仍按本批第 7 项处置单列，未在此扩建。
本轮只判断后续二十项是否可继续，不自动派发。
