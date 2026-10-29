# 98f2a2e6 后二十项本地交付

本批固定20项、73条 item/path 足迹。上游从 `98f2a2e60b6a6da3906628a157c1751ee439e5ca`（不含）到 `aab01af6f298ac175a2a3b0b5636d47c385f091e`，直接后续对照上界为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`；没有 fetch、追随后续 HEAD 或开始下一批。

后端源组合已由实际 `main` 接受，I 共享源提交为 `e24f7becd5e9d8482b5b82ec43ab828e0f9f64d9`；Web actual `master` 与已提交 `.github/consumer-web.json` 同为 `13dd58384a29d377b2fd50395314376999d71325`。最终文档提交和完整 SHA 以本批外部交付回执为准。

本批范围内的实现、定向组合与恢复清理已完成。扩大运行的 integration harness 为46通过、1个已确认的既有基线失败；不能称全局门禁全绿。未 push、未部署，远程 `ci-required` 尚未运行。

## 冻结与证据入口

- 本地证据根：`/Users/xldu/.local/state/multirag/validation/20261029-batch-after-98f2a2e6`。
- `scope.json` SHA256：`78f4487f59a74432c082a00bcc6da25695eee93cafa89f363ca14c4093ef86ab`；后端基线 `25308876e6bda991b51e3701074870293fad686e`，Web基线 `fcf2a372ba7ab545626ec20b267740a71c051004`。
- 逐足迹最终矩阵：`integration/I/final-path-matrix-v2.json`，SHA256 `ad3bcc5f645e40be7d75c9e6d7134f270011c56e971959569e5422bdc527abd0`。233个原始 hash/JSON pointer 引用已核验；历史候选 ready/pending 字段保留为捕获状态，不能代替当前接受状态。
- 最终源码冻结：`integration/I/final/source-inputs-v4.json`（3021个源码/fixture/config/lock输入）；早期v1/v2/v3保留。共享源接受：`integration/I/shared-source-acceptance.json`。
- 完整接受映射、原始运行索引、登记偏差、独立读回与最终SHA，分别见 `acceptance-ledger.json`、`raw-run-index.json`、`final-validation.json`、`delivery-complete.json` 和 `latest-progress.json`。这些是本机证据文件，不是远程 CI 或供应商运行凭据。

## 逐项处置与质量比较

| # | 固定上游 | Owner | 本地结论与依据 |
|---|---|---|---|
| 1 | `96a416629d764141bc7cfb1235f5a933d41edb50` | G | Go metadata 从生产者到真实过滤、flatten 和生成消费者统一为 common.MetaData；保留 bool、0、精确整数，先授权后按真实 tenant/index 分组。 |
| 2 | `eee6ad546f6bfb1a799b6f80cb8d74f6d0f2fd72` | G | 补充严格、不可变的引用 metadata resolver；区分 null/空数组，拒绝畸形来源与歧义，显式选择不会回退扩大。 |
| 3 | `7789862cc5956f3365a340e60d299bf78c473808` | I | ES 服务增加有界 /tmp tmpfs，保留 image/data/profile；本机缓存 ES8.11.3 原 /tmp 已为1777且 here-doc 成功，因此属于配置加固，没有复现当前权限故障。 |
| 4 | `794c1f4b251bf2536300afb2da1b0a9c3f524410` | M | Python Chat/CV/Embed 共用严格 Ark key/envelope 解析；Chat/CV 使用 envelope endpoint，Embed 保留显式 model_name。连接验证、原子创建与数值模型 ID 的实际消费者贯通；保留 typed DTO/atomic 合同。 |
| 5 | `bd49fd70aaff43b70863ba1a430630c5783a869d` | F | 已等价且本地更强：保留可信 MIME、no-store/nosniff、授权下载与真实存储夹具。无业务差异时不制造补丁，以现有实际 HTTP/SQL/MinIO 和 Web 下载消费者证明。 |
| 6 | `bf6c091c9f6a6b00d90add458c24e1a34e759d24` | K | Go KG scoring 保留输入所有权，严格有限分数、确定性 tie-break 与限额；不移植会修改调用者数据或吞掉无效分数的行为。 |
| 7 | `a237a89b90932c41b17a39a7ad109edc626e7590` | K | Go rewrite prompt 使用明确值边界；响应解析拒绝重复/畸形结构、错误数字与歧义，保留合法空值、数组和取消。 |
| 8 | `431f52a5d478c38a5fb959001ab1faa9fa9a407b` | A | Go actual Router 提供 GET /agents/templates；多语言对象与完整 DSL 不压平，以时间+ID 稳定排序。Python 现有目录同步补齐稳定排序，合法0/false/[]保留。 |
| 9 | `9f3e289b7803c1b0b4873a89586b1ec9077ea57f` | D | 生产 Markdown parser 在 fenced code 之外保护 Markdown/HTML table 范围；支持一条或多条分隔线、空单元格和自定义 delimiter，naive/Flow/图片元数据链回归。 |
| 10 | `4cbe597d7ee98fc34bfc29bf44f9c1f036c92bef` | F | Dify 改用 current_user/严格 Principal，并保持请求 Session 与 Dify 错误 envelope；只映射对应的未认证异常，活动 token_required 共享 helper 保留。 |
| 11 | `3b1ae3f829add2eb71ba4fd36a09716685145ad3` | K | 保留较强 SelectFields；真实 ES 消费 Fields/OrderBy/fusion/阈值，metadata-only 解码保留精确整数。Infinity 修正显式 fusion 的 dense filter/相似度，保留 legacy 路径；仅 SDK 构造层证明。 |
| 12 | `ee32d91aab160420e9f49fd24cd3a840c7c83405` | G | 补充授权范围内的文档 metadata enrich 原语：KB→真实 tenant/index、document key 与 nil/[]一致，失败或取消不返回部分结果，不修改 chunks 输入。 |
| 13 | `f6ff862a241f7d17c27582aaa9a6370c9353fe4c` | G | 保留既有更强的大小写不敏感 matcher、精确数值和运算符约束；typed 消费链收敛，所有模式与基础文档选择相交，非法输入先于模型/引擎调用。 |
| 14 | `71649db3b065a5e355b536362bab048e931c745b` | F | 继续使用共享 think 状态机，窄修 pending close 后的空白处理；覆盖任意分片、重复尾文与历史 SDK 完成消费者，不恢复旧 last_idx 拼接。 |
| 15 | `4b2af1347c21499346dbf3ae953cb5414615971a` | A | Go PUT tags 使用严格对象与 string/list 校验、个人 owner 锁内重验和原子写；[]清空，失败零写。Python 复用持久字段并修正真实修改时间，published version 与团队资源保持原生命周期。 |
| 16 | `e629c0203b3f63050c19002ee35bfb5dfc65c2cd` | K | Go 提供 entity/relation/community 搜索准备原语；完整 KB 授权后使用真实 tenant/index、确定性排序与不可变拷贝。未接入完整 UseKG/KGSearchRetrieval，不将原语测试宣传为完整 KG 产品交付。 |
| 17 | `1d7e45115b7b810125dfd2b4428ba34c3d5b1b40` | C | Salesforce 静态 CRUD、factory、worker 和实际 Web 表单贯通；canonical HTTPS origin、防 redirect/跨源，省略/null 默认与[]/空串零请求分开。完整页计数、秒级 SOQL 外扩与精确窗口内筛选、失败 checkpoint 和取消排空均验证；不支持删除同步。 |
| 18 | `b8db200757812e72af756d56d717ece0ba29f725` | S | Go actual Router 提供 MCP import/test/tools；保留 CRUD，严格 headers/JSON，active personal owner 在 outbound 前及事务锁内重验。公网 DNS 全量校验与 pin、无 proxy/redirect；HTTP/SSE/session/分页/心跳/取消有界，单条失败不部分写。 |
| 19 | `f78ef328bb8d8dc87ab2aaf86f0d122f4a5b9b97` | M | Go Bedrock Titan/Cohere embedding 贯通 catalog→实例→签名 HTTP 与实际消费者；严格重复 JSON、数量/维度/有限值、批次顺序、上下文取消及实例隔离。不扩模型接口或依赖，不声明真实 AWS 验证。 |
| 20 | `aab01af6f298ac175a2a3b0b5636d47c385f091e` | I | 只在 Docker build 副本规范 Python 包源、index 和 graspologic GitHub URL，pyproject/lock 两副本一致；保留 root pin/revision 与用户 mirror 默认。本地无 release.yml，记录不适用，不新增 release workflow。 |

## 73条上游足迹与当前落点

下表逐条保留上游 item/path 身份；Web 的 `web:` 前缀指独立 Web 仓库。精确候选/实际提交 blob、质量比较、首错与验证引用在最终矩阵的同一 item/path 中，不能以文件存在替代行为验收。

| # | 上游路径 | 当前本地入口 | 处置 |
|---|---|---|---|
| 1 | `internal/service/metadata.go` | `internal/service/metadata.go`<br>`internal/service/metadata_context.go` | 适配并验证 |
| 1 | `internal/service/metadata_filter.go` | `internal/service/metadata_filter.go` | 适配并验证 |
| 1 | `internal/service/metadata_filter_test.go` | `internal/service/metadata_filter_test.go`<br>`internal/service/metadata_directory_test.go`<br>`internal/service/after98_metadata_live_test.go` | 适配并验证 |
| 2 | `internal/common/reference_metadata.go` | `internal/common/reference_metadata.go` | 适配并验证 |
| 2 | `internal/common/reference_metadata_test.go` | `internal/common/reference_metadata_test.go` | 适配并验证 |
| 3 | `docker/docker-compose-base.yml` | `docker/docker-compose-base.yml` | 适配并验证 |
| 4 | `api/apps/services/provider_api_service.py` | `api/db/model_contract.py`<br>`api/db/services/model_provider_service.py`<br>`api/db/services/provider_verification.py`<br>`api/db/services/provider_model_resolution.py`<br>`api/db/services/tenant_llm_service.py` | 保留较强本地合同；按范围补验证 |
| 4 | `rag/llm/chat_model.py` | `core/llm/chat.py`<br>`core/llm/volc_auth.py` | 适配并验证 |
| 4 | `rag/llm/cv_model.py` | `core/llm/cv.py`<br>`core/llm/volc_auth.py` | 适配并验证 |
| 4 | `rag/llm/embedding_model.py` | `core/llm/embedding.py`<br>`core/llm/volc_auth.py` | 保留较强本地合同；按范围补验证 |
| 5 | `api/apps/restful_apis/document_api.py` | `api/apps/restful_apis/document_download_api.py`<br>`api/utils/web_utils.py`<br>`api/apps/services/document_file_http.py` | 保留较强本地合同；按范围补验证 |
| 5 | `test/testcases/restful_api/test_documents.py` | `tests/integration/test_document_file_http.py`<br>`tests/integration/test_beta_document_download.py` | 保留较强本地合同；按范围补验证 |
| 5 | `test/testcases/test_http_api/test_file_management_within_dataset/test_doc_sdk_routes_unit.py` | `tests/unit/test_sdk_document_download_mime.py` | 保留较强本地合同；按范围补验证 |
| 5 | `test/unit_test/api/apps/restful_apis/test_openai_stream_no_duplicate.py` | `tests/unit/test_dialog_final_answer.py`<br>`tests/unit/test_sdk_session_completions.py` | 保留较强本地合同；按范围补验证 |
| 6 | `internal/common/kg_scoring.go` | `internal/common/kg_scoring.go` | 适配并验证 |
| 6 | `internal/common/kg_scoring_test.go` | `internal/common/kg_scoring_test.go` | 适配并验证 |
| 7 | `internal/common/kg_query_rewrite.go` | `internal/common/kg_query_rewrite.go` | 适配并验证 |
| 7 | `internal/common/kg_query_rewrite_test.go` | `internal/common/kg_query_rewrite_test.go` | 适配并验证 |
| 8 | `internal/dao/canvas_template.go` | `internal/dao/canvas_template.go` | 适配并验证 |
| 8 | `internal/handler/agent.go` | `internal/handler/agent.go` | 适配并验证 |
| 8 | `internal/handler/agent_test.go` | `internal/handler/agent_catalog_tags_live_test.go` | 适配并验证 |
| 8 | `internal/router/router.go` | `internal/router/router.go` | I共享提交接线；实际Router/JWT/API/SQL验证 |
| 8 | `internal/service/agent.go` | `internal/service/agent.go`<br>`api/db/services/canvas_service.py`<br>`api/apps/restful_apis/agent_api.py`<br>`tests/integration/test_agent_tags_atomic.py` | 适配并验证 |
| 9 | `deepdoc/parser/markdown_parser.py` | `deepdoc/parser/markdown_parser.py` | 适配并验证 |
| 9 | `test/unit_test/deepdoc/parser/test_markdown_parser.py` | `tests/unit/test_markdown_tables.py` | 适配并验证 |
| 10 | `api/apps/restful_apis/dify_retrieval_api.py` | `api/apps/restful_apis/dify_retrieval_api.py`<br>`tests/integration/test_dify_auth_http.py` | 保留较强本地合同；按范围补验证 |
| 10 | `api/utils/api_utils.py` | `api/utils/api_utils.py` | 保留较强本地合同；按范围补验证 |
| 10 | `test/testcases/restful_api/test_dify_retrieval_routes_unit.py` | `tests/unit/test_dify_retrieval_contract.py`<br>`tests/integration/test_dify_auth_http.py` | 保留较强本地合同；按范围补验证 |
| 10 | `test/testcases/test_http_api/test_dataset_management/test_dify_retrieval_routes_unit.py` | `tests/unit/test_dify_retrieval_contract.py`<br>`tests/integration/test_dify_auth_http.py` | 保留较强本地合同；按范围补验证 |
| 10 | `test/testcases/test_http_api/test_file_management_within_dataset/test_doc_sdk_routes_unit.py` | `tests/unit/test_sdk_document_download_mime.py`<br>`tests/integration/test_document_file_http.py` | 保留较强本地合同；按范围补验证 |
| 10 | `test/testcases/test_http_api/test_session_management/test_session_sdk_routes_unit.py` | `tests/unit/test_async_auth_deps.py`<br>`tests/unit/test_sdk_session_authorization.py` | 保留较强本地合同；按范围补验证 |
| 10 | `test/testcases/test_web_api/test_dataset_management/test_dataset_sdk_routes_unit.py` | `tests/unit/test_dataset_auth.py`<br>`tests/unit/test_async_auth_deps.py` | 保留较强本地合同；按范围补验证 |
| 10 | `test/unit_test/api/apps/sdk/test_dify_retrieval.py` | `tests/unit/test_dify_retrieval_contract.py`<br>`tests/integration/test_dify_auth_http.py` | 保留较强本地合同；按范围补验证 |
| 11 | `internal/engine/elasticsearch/chunk.go` | `internal/engine/elasticsearch/search.go` | 保留较强本地合同；按范围补验证 |
| 11 | `internal/engine/elasticsearch/kg_test.go` | `internal/engine/elasticsearch/kg_test.go` | 保留较强本地合同；按范围补验证 |
| 11 | `internal/engine/infinity/chunk.go` | `internal/engine/infinity/search.go` | 保留较强本地合同；按范围补验证 |
| 12 | `internal/service/enrich_metadata_test.go` | `internal/service/enrich_metadata_test.go`<br>`internal/service/after98_metadata_live_test.go` | 适配并验证 |
| 12 | `internal/service/metadata.go` | `internal/service/enrich_metadata.go`<br>`internal/service/metadata_context.go` | 适配并验证 |
| 13 | `internal/common/metadata_utils.go` | `internal/common/metadata_utils.go` | 保留较强本地合同；按范围补验证 |
| 13 | `internal/common/metadata_utils_test.go` | `internal/common/metadata_utils_test.go` | 保留较强本地合同；按范围补验证 |
| 13 | `internal/service/metadata_filter.go` | `internal/service/metadata_filter.go` | 保留较强本地合同；按范围补验证 |
| 13 | `internal/service/metadata_filter_test.go` | `internal/service/metadata_filter_test.go` | 保留较强本地合同；按范围补验证 |
| 14 | `api/db/services/dialog_service.py` | `api/db/services/dialog_service.py`<br>`tests/unit/test_dialog_final_answer.py` | 保留较强本地合同；按范围补验证 |
| 15 | `internal/dao/user_canvas.go` | `internal/dao/user_canvas.go` | 适配并验证 |
| 15 | `internal/entity/canvas.go` | `internal/entity/canvas.go` | 保留较强本地合同；按范围补验证 |
| 15 | `internal/handler/agent.go` | `internal/handler/agent.go` | 适配并验证 |
| 15 | `internal/handler/agent_test.go` | `internal/handler/agent_catalog_tags_live_test.go`<br>`internal/service/agent_tags_test.go`<br>`tests/integration/test_agent_tags_atomic.py` | 适配并验证 |
| 15 | `internal/router/router.go` | `internal/router/router.go` | I共享提交接线；实际Router/JWT/API/SQL验证 |
| 15 | `internal/service/agent.go` | `internal/service/agent.go`<br>`api/db/services/canvas_service.py` | 保留较强本地合同；按范围补验证 |
| 16 | `internal/service/kg_search.go` | `internal/service/kg_search.go` | 适配并验证 |
| 16 | `internal/service/kg_search_test.go` | `internal/service/kg_search_test.go` | 适配并验证 |
| 17 | `common/constants.py` | `common/constants.py` | 适配并验证 |
| 17 | `common/data_source/__init__.py` | `common/data_source/__init__.py` | 适配并验证 |
| 17 | `common/data_source/config.py` | `common/data_source/config.py` | 适配并验证 |
| 17 | `common/data_source/salesforce_connector.py` | `common/data_source/salesforce_connector.py` | 适配并验证 |
| 17 | `rag/svr/sync_data_source.py` | `core/svr/sync_data_source.py` | 适配并验证 |
| 17 | `web/src/assets/svg/data-source/salesforce.svg` | `web:src/pages/settings/datasource/constants/index.tsx` | 适配并验证 |
| 17 | `web/src/locales/en.ts` | `web:src/locales/en-US/datasource.ts` | 适配并验证 |
| 17 | `web/src/locales/zh.ts` | `web:src/locales/zh-CN/datasource.ts` | 适配并验证 |
| 17 | `web/src/pages/user-setting/data-source/constant/index.tsx` | `web:src/pages/settings/datasource/__tests__/salesforce.test.tsx`<br>`web:src/pages/settings/datasource/constants/default-values.ts`<br>`web:src/pages/settings/datasource/constants/form-fields.tsx`<br>`web:src/pages/settings/datasource/constants/form-values.ts`<br>`web:src/pages/settings/datasource/constants/index.tsx`<br>`web:src/pages/settings/datasource/constants/salesforce.ts`<br>`web:src/pages/settings/datasource/types.ts` | 适配并验证 |
| 18 | `internal/handler/mcp.go` | `internal/handler/mcp_discovery.go`<br>`internal/handler/mcp_discovery_test.go`<br>`internal/handler/mcp_discovery_live_test.go`<br>`internal/handler/mcp_discovery_listener_test.go` | 保留较强本地合同；按范围补验证 |
| 18 | `internal/mcpclient/client.go` | `internal/utility/mcp_client.go` | 适配并验证 |
| 18 | `internal/mcpclient/client_test.go` | `internal/utility/mcp_client_test.go` | 适配并验证 |
| 18 | `internal/router/router.go` | `internal/router/router.go` | I共享提交接线；实际Router/JWT/API/SQL验证 |
| 18 | `internal/service/mcp.go` | `internal/service/mcp.go`<br>`internal/service/mcp_discovery.go`<br>`internal/service/mcp_headers.go` | 适配并验证 |
| 18 | `internal/service/mcp_test.go` | `internal/service/mcp_discovery_test.go` | 适配并验证 |
| 18 | `internal/utility/ssrf.go` | `internal/utility/ssrf.go` | 保留较强本地合同；按范围补验证 |
| 18 | `internal/utility/ssrf_test.go` | `internal/utility/ssrf_test.go` | 适配并验证 |
| 19 | `conf/models/bedrock.json` | `configs/models/bedrock.json` | 适配并验证 |
| 19 | `internal/entity/models/bedrock.go` | `internal/entity/models/bedrock.go`<br>`internal/entity/models/bedrock_embedding.go`<br>`internal/entity/models/bedrock_json.go` | 保留较强本地合同；按范围补验证 |
| 19 | `internal/entity/models/bedrock_test.go` | `internal/entity/models/bedrock_embedding_test.go`<br>`internal/entity/models/bedrock_json_test.go`<br>`internal/service/model_service_bedrock_embedding_test.go` | 保留较强本地合同；按范围补验证 |
| 20 | `.github/workflows/release.yml` | `Dockerfile`<br>`scripts/build_docker_image.sh` | 本地无该workflow，保留依据 |
| 20 | `Dockerfile` | `Dockerfile` | 适配并验证 |

## 候选到实际主干接受

| Owner/候选线 | 候选SHA | 实际接受SHA |
|---|---|---|
| A | `b4cdacbade2e47dc719b290948d390db0878b503` | `719aa2edbdcf61cbc70e4481d845d7982a4e5933` |
| C | `43c6ba682ba7c3bf511bb61dae97087a634e86f9` | `97f80890a0c335c47a8ca96ff1efebffdb3905d3` |
| D | `5c0b2a3677d37a18ac4b6ee11e0166b6ec5b463f` | `da325f7f036a96f887d155fa4c7327f5b2c67628` |
| F | `23dd27543677f69cc319864560b0f529739644a4` | `788297ba86e5ac075dcd6e21edd7ae38e39b956b` |
| G | `020c036f3abc5e25aeacec1ec694918068b88f55` | `7134440f4e35ab0802f289478ae214f596d16569` |
| I | `130f16c0dab09690f491f79fb26949019fe3f419` | `afd84550d44da0e31c520628e20f174e2b83490e` |
| K | `19732207b34e04705f544cf0027fa80e6826d7d1` | `39b18bcc051108b68e0f87fb18db21d5cec630d3` |
| M-go | `492fb6d1977a05bd92f2a5e4a08afb3a7b7b7e2c` | `0b024637f1fe290c7c852e3b9e6dd79ce6a860d9` |
| M-python | `2a08cac627ea04b5b61f7c6c4d6afa7a01b62b0d` | `ba52eee36048c3f854db5e2ea83233bf5ace27e0` |
| S | `828437e2f1267a879cfd9aeb67da51b5f3898aba` | `1e1b34822fac9b588d16f160633b1d6a299cd215` |
| W | `13dd58384a29d377b2fd50395314376999d71325` | `13dd58384a29d377b2fd50395314376999d71325` |

I后续共享接线、typed origin、测试夹具、SQL行登记、服务/consumer分类和pin的实际提交为 `e24f7becd5e9d8482b5b82ec43ab828e0f9f64d9`。候选恢复证明仍绑定原候选字节，I后续补修单独关联最终输入和复跑，不回填候选证明。

## 实际验证及复用边界

| 层 | 最终本地证据 | 范围与限制 |
|---|---|---|
| Python 定向单测 | `7bc1e2b469424a738e9034fe2cd93f7b`：594通过、无skip | 15个真实受影响文件；Salesforce guard修后82条子集在 `a3faf770e37249cd9174fab7bc8a46fd` 再通过，不能累加为676个不同用例。 |
| Python HTTP/SQL | `80906dcb840247f58a57ce9a63b652ba`：21通过 | Dify真实身份/SQL14、Volc3、Agent原子标签/目录4；SQL固定行在使用前登记，提交/拒绝/清理独立读回。 |
| 共享Provider夹具回归 | `ee70cda1e3af43938032f491bdc6ff49`：31通过 | fixture从integration模块移到tests/support后，原Provider与Volc测试断言均保留。Volc3与上行重叠，分别保留输入版本。 |
| Salesforce真实消费者 | `230aafbc53e544cab18677644d2b41c1`：4通过 | 3项受控worker+真实SQL/MinIO/Milvus边界，1项actual Web13dd APIClient+独立SQL读回；不是真实Salesforce。 |
| Python静态 | Ruff受影响28文件、后续5个fixture文件与Salesforce叶子；8条import契约、async DB检查通过 | Salesforce/constants定向mypy最终通过。首次mypy路径拼写退出2、随后optional origin退出1均保留；不是全量mypy结论。 |
| 扩大harness | `dc9375c1d0a94a14ab095176d95acbab`：46通过、1失败，无skip | Search consumer既有test-to-test import：base与当前blob均 `c9d8b12d7bf637ebf28d63c053420bb636a498fe`，未扩修、未加豁免或削断言。Salesforce consumer漏登记原失败 `9232736d2e8a45eca8ca5961ce5e1853` 已修复并通过。 |
| Go组合race | `92f9a3ca62e64c22b9fcbbf4b843eef9`：9包、100顶层、218通过事件，无skip | 99个定向合同加actual Router JWT/API/真实PostgreSQL。子测试事件不是额外顶层数。G/K/S/A独立SQL/ES层按实际输入/consumer闭包复用，不用注入handler替代Router鉴权。 |
| Go构建/链接 | `b74a91a1122641fbbd2120df4f2a7f07`：6命令通过 | version、internal build、vet、server_main/admin_server/multirag_cli三个独立native链接；只复用已校验native archive，没有clean native rebuild。 |
| Web | final13dd定向6例/静态/构建及C/A/M真实消费者；`edd76f289f4c4428951120d74125251a` 严格预登记build通过 | 历史acf组合test:ci的990 source+1118 Vitest+93 desktop+21tooling结果只按精确输入闭包复用，不能称最终13dd全量test:ci新运行。2595个源码与13dd blob/master相等，浏览器仅两张标注contact sheet审阅。 |
| 下载、存储与其他owner层 | 逐项原始输入、JUnit、命令退出和清理见最终矩阵及owner handoff | 保留更强现有实现；实际SQL/ES/MinIO、受控HTTP与原生SDK构造分别标注，不将本地fixture宣传为供应商或生产证明。 |

## 原失败、恢复与清理

候选均先验证bundle及独立还原，再接受实际main/master；无alternates的base-only还原先证明候选不可见，再导入bundle。M早期alternates还原和C shared clone仅作历史文件比较，不能替代后续严格恢复证明。W另有完整自包含bundle；原始raw与版本均保留。

11套候选tree/ref/admin均已精确清理和独立读回（M分Python/Go）；M-Go实际由M先执行清理，I只独立确认，账本如实保留执行者。其余借用/旧managed工作树和原28项WIP保留；compose与PROGRESS按本批精确hunk反向投影核对。

最终I运行的独立读回 `b40657adee1344bc8509696c7d8402a6` 共257项：37个scratch DB、146个监听、4个MinIO桶及4个对象、4个Milvus collection、50个限定owner Redis scope、进程组和8个临时路径均不存在；Redis没有登记写键，不制造零写之外的成功声明。I私有Go modules/build缓存、Python runtime链接/日志及私有配置随后按登记根清理，日志只保留实际字节，凭据只留hash；独立清理回执见最终索引。

历史偏差独立列出，不回填为预登记：A/G/M-Go早期空树先创建后补登记并重建；F tiktoken根缓存首次使用未单独预登记；A早期Infinity字典复制缺逐文件hash；W早期build遗漏desktop network-policy输出路径。F实际缓存字节与清理失败、W历史覆盖字节未知及I首次清理保护失败都保留。W严格预登记修复build在I删树前完成，原时间顺序可读回。前批21份历史manifest缺失不与本批原始版本审计混算。

未覆盖：远程CI/merge queue、部署/生产、真实Ark/AWS/Salesforce供应商、常驻worker长时间运行、Infinity真实服务及native clean rebuild。后续跟进以本批最终回执为起点，不自动开始下一批。
