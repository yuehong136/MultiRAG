# 技能资产库交付状态

2026-10-04：用户确认的本地独立资产库已实现，Python/Go原生后端与共用Web/CLI已提交。
本期不含远程技能源和Agent自动执行。行为真值见[合同](CONTRACT.md)，
部署顺序与故障恢复见[运行说明](README.md)。

| 单元 | owner | 状态/依赖 |
|---|---|---|
| 共同合同、集成、CLI | root | 合同c8087791；CLI0342420b，test/vet/build及两端真实HTTP消费通过 |
| Python/schema/migration | backend_space + root | c2a7d344，已实现，最终真实集成30项通过 |
| Go backend/Milvus | search_models | a940e8a3，已实现，真实集成8项及SQL故障回归1项通过 |
| 双端101项分页验收 | search_models | bc1c9ccd，纯测试补充，真实HTTP通过 |
| Web | web_cli | 326f474897150ce7ccb5111ab6438910bbac6c9b，完整门禁与Python浏览器闭环通过 |
| 文件批删前置 | 独立批删工作线 | 正式bdbe93e0、c039e005已接收；见文件删除合同与验收记录 |
| Go模型前置 | 独立模型工作线 | 已交接4ba652f3，GetEmbeddingModel/GetRerankModel保持签名；本功能精确按tenant_llm ID绑定 |

## 范围

首期：本地目录/ZIP、不可变版本、显式活动版本、空间CRUD、目录/下载、安装卸载、
配置/索引/检索、Web/CLI。外部技能源后续独立处理；不接Agent自动执行。
每个可交付单元独立验证和提交。未push、未部署、未操作生产数据；其他工作线改动保留。

| 能力 | 复用与新增 | 验收重点 |
|---|---|---|
| 空间CRUD/权限 | 复用JWT/APIkey；新增独立租户空间、固定owner与revision | 租户隐藏、跨owner拒写、重名/并发、混合owner批删 |
| 目录/版本/上传下载 | 复用File/MinIO；新增不可变manifest、SemVer、活动版本、路径规范化 | 根SKILL.md、大小/hash、嵌套同名文件、二进制和ZIP精确读回 |
| 安装/卸载/删除 | 接正式文件批删；新增持久operation、幂等别名、lease/fencing和对象快照 | 隐藏先于清理，丢File行、半上传、失败重试、父子删除及失租恢复 |
| 索引/检索 | 复用模型接入；新增独立Milvus generation、实际SDK、严格验证/CAS | 三种模式、字段权重、换维、旧generation保护、孤儿回收 |
| 模型配置 | 复用租户模型表；精确BIGINT ID绑定且JSON始终字符串 | 超2^53 ID、真实HTTP模型协议、rerank坏200不可假成功 |
| Web | 复用共享UI/Query/i18n/APIClient；新增/skills资产库 | EN/ZH、明暗主题、390px、键盘、上传检索删除、原失败任务Retry |
| CLI | 复用鉴权/HTTP/参数入口；新增skills命令 | 参数中空格、目录manifest、状态等待、错误退出、下载不覆盖 |
| 架构与部署 | 两端共用Python管理的7表；原生实现、空间单owner写 | 双向跨读索引/对象/AES字节，Python生产入口无需Go |

原有Web“技能”是MCP工具选择，不是SKILL.md资产库。现有File、模型与CLI基础设施
可复用；空间领域、版本状态、搜索generation和恢复账本均需新增。
不复制跨后端HTTP转发、混合nginx分流或上游filesystem命令重构。

## 证据

- 最终`make verify` exit0：Ruff/import/async DB门禁、mypy137源文件及
  **5433 unit passed**（51.87s）。日志`/tmp/multirag-skills-verified-delivery.log`。
  中间一次因并行FuturMix测试/目录未同步失败2项；该owner提交788c0b19后复跑全绿，
  本任务未改写其测试或目录，也未以忽略门禁替代。
- `make integration INTEGRATION_WORKERS=0`选定Skills、File批删、DB bootstrap：
  **30 passed，0 skip**（65.44s），证据`.test-results/20261004-214917-18776/`。
  包含后台start/stop与HTTP轮询，该生命周期用例不靠手动run_once推进。
- Go正式集成**8 passed，0 skip**（113.47s），证据
  `.test-results/20261004-214925-18887/`；最后SQL错误不得伪装空结果的故障注入
  **1 passed**（30.87s），证据`.test-results/20261004-215207-21098/`。
- 双端101项资产分页**1 passed**（22.21s），证据
  `.test-results/20261004-215657-22957/`：GET列表及空keyword query均100/1/0、total101，
  deleting和其他租户不计入，不受top_k10影响。该SQL seed只测元数据分页，上传另由端到端用例验证。
- Go四包测试、全部internal vet/build、三个独立main构建通过；CLI定向test/vet/build
  通过，工具链Go1.25.14。仅已有go-m1cpu C编译warning。
- `make smoke`对隔离完整FastAPI监听端口：ping200、healthz200，DB/Redis/索引/存储均ok；
  日志`/tmp/multirag-skills-smoke.log`。业务码与读回由上述HTTP用例验证。
- Web `test:ci` **1187 passed**（688 Node、408 Vitest、81 Desktop、10 tooling）；
  API211、产品UI33、build、i18n、file-size、bundle budget通过。全库lint0 errors，
  1454项存量warning；新增代码scoped lint干净。
- 浏览器真实Python+JWT+隔离SQL/MinIO/Milvus闭环通过。先删非活动版本再卸载暴露的
  子版本状态回退已修；原失败operation点击Retry后attempts=2/succeeded，检索total0、
  已删版本files404。下载/ZIP逐字节一致，模型ID无精度丢失，页面错误0。
  `/tmp/skills-web-contact-sheet-final.png`已拼图审阅，HTTP记录
  `/tmp/skills-web-ui-evidence.json`不含token；fixture已退出并删除私有凭证文件。

真实基础设施为PostgreSQL17.11、MinIO、Milvus服务自报3.0-beta（Python SDK2.5.11）；
仅使用隔离资源且fixture验证清理。此证据不等同于Milvus3.0.2或生产集群认证。

文件删除前置合同见[文件删除](../references/file-deletion.md)，正式证据见
[删除验收](../ragflow-porting/file-deletion-acceptance.md)。该能力跨存储非原子，
Skills独立保存对象地址和索引generation清理记录，不能仅靠重发File ID判断恢复。

## 剩余限制与风险

GitHub/ClawHub/skills.sh、远程下载和信任策略、force覆盖、Agent自动执行另立范围；
无技能执行沙箱承诺。空间owner不支持在线迁移。

已验收组合为PostgreSQL+MinIO+Milvus。ES/Infinity、无严格对象缺失读回的存储明确不可用。
Go支持AES128/256跨读，SM4明确不可用。rerank仅严格验证的OpenAI-compatible/VLLM协议；
Go embedding仅已有实际Encode驱动的provider。

外部模型使用本地HTTP协议替身，含错误注入和真实请求，不代表真实付费provider验收。
完整浏览器流程针对Python；Go通过HTTP/CLI及双向跨读，未另跑Go浏览器全流程。
集成为上述选定范围，不声称跑遍全库所有集成文件。

SQL、对象和索引间无分布式事务。失败保持隐藏、持久恢复地址并显式重试；故障可能
已部分生效，不能根据HTTP202判定完成。部署先迁移并核对能力，非空技能表/operation
禁止破坏性downgrade。
