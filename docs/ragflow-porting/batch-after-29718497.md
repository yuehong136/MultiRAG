# 29718497 后二十项本地交付

2026-11-06 冻结第一父序列 `(2971849783b879749ae25732f202e42acf0a3943,22a058f56cb39334539eea08ea37e243faf61a5d]`，连续 20 项、211 条逐提交路径、195 个去重路径；第 19 项的三次重命名按目标路径各计一次。参考上限为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch、未扩批。后端基线为 `42a54005d069aa9b818992d38f5253387a908d58`，Web 基线为 `3e473d614e70e9ccdd57c4ea4c715833effbd6e2`。

最终可执行组合为 `602c994c7a6597fde520a188a53cf2e5a1179c9c`，其后仅补本记录。独立 Web/master 与 `.github/consumer-web.json` 均固定为 `5b577777ca8298ed916ecddff75388d5aeb6b321`。本地证据根为 `/Users/xldu/.local/state/multirag/validation/20261106-batch-after-29718497`；`item-ledger.json` 保存全部路径的决定、质量比较与证据，`intake.json` 保存候选到主分支映射，`validation.json`、`cleanup.json` 和 `delivery-complete.json` 保存最终验证与退役结果。评估完成不表示未采用的上游能力已实现。

## 逐项处置

| 项 / 上游 SHA | 本地行为与质量选择 |
|---|---|
| 1 / `70ae25fc` | Web 检索与搜索改用独立 Top 数量，过滤/重试保留选择；`size` 与候选 `top_k` 分开。保留后端真实候选 total，不采用已被后续修正的 `len(chunks)`；其他 REST/SDK/CLI 分页仍按实际消费者使用。搜索过滤不会重跑摘要模型。 |
| 2 / `49ef9599` | 增加韩语预览，25 个 namespace 按需加载；5346 个当前叶键中 1406 个翻译、3940 个明确英文回退，逐键覆盖与占位符一致。未宣称完整翻译或母语审校完成。其他旧版语言列表不搬入当前产品。 |
| 3 / `9614605b` | 保留 typed 模型记录到 numeric/name/default resolver、bundle 与 prompt budget 的 `max_tokens` 传播；补真实提交读回及合法零值回归，不新增重复 JSON 配置。 |
| 4 / `9d595096` | 保留从实际模型记录取得 `is_tools` 的链路，显式 false 不回退目录默认；补单测及 SQL/bundle 回归。 |
| 5 / `312514c0` | Go 注入目录的 `max_dimension` / `dimensions` 元数据进入发现、按名称/ID 的 embedding wrapper 及实际推理校验；授权后、HTTP 前拒绝非法维度，0 使用供应商默认，返回元数据深拷贝。 |
| 6 / `290432d1` | 保留当前认证 mindmap API、非空 saved search ID、分享与取消链路；补真实 API adapter 的请求身份、取消和搜索消费者回归。 |
| 7 / `7efa481d` | 完整审阅发布草稿，按当前模块文档核对实际能力，不将其发布为 MultiRAG release 或扩展到草稿中的全部特性。 |
| 8 / `92c4b768` | 保留本项目独立 0.9.9 版本及锁文件；仅修正 admin client README 的自动监控/重启表述，描述实际按需健康检查及 restart/shutdown 501 边界。 |
| 9 / `9c30557e` | 补 VolcEngine 维度请求及 Replicate 原生模型 ID 优先、owner/name 回退；保留已有严格批次、索引、有限值、输入类型和取消处理，拒绝重复目录行的上游回归。未扩展自定义模型维度持久化合同。 |
| 10 / `daa38111` | 全部 61 条路径逐项评估。Aliyun/Gitee/SiliconFlow/ZHIPU/LM-Studio 的剩余客户端复用共享 transport；LM-Studio/ZHIPU 补完整 SSE 帧和有界 JSON。保留原生协议解码、终止语义、错误与 body 关闭，不采用忽略坏事件的通用解析或无人调用的 helper。 |
| 11 / `ec89fc03` | 保留现有移动设置 ContextRail/drawer，实际验证 390px、44px 操作区、当前项、Escape、焦点返回与退出入口；不恢复旧 sidebar。 |
| 12 / `bde2b1fc` | Python embedding 统一严格批次与全局维度/基数/顺序/有限值验证，失败不返回部分成功或错误计费。修复供应商 token/truncation、SDK 状态识别与有限重试；保留 Replicate deadline、Azure key 和资源关闭。真实供应商账号未验收。 |
| 13 / `bae8c6f1` | 使用已有 `docx-preview` 0.3.7 替换 Mammoth，移除其依赖闭包；认证 ArrayBuffer 保持。DOMPurify、CSP 与无脚本 iframe 呈现来源分页、缩放及宽页/CJK；实际两页和末尾标记可见，格式切换释放 blob。不宣称任意 Word 自动分页与桌面软件完全一致。 |
| 14 / `0d836afd` | 当前 n-hop 已保留重复边最大 pagerank，保留实现并覆盖负值、零及重复输入；不以累加替代最大值。 |
| 15 / `de06c9a6` | Langfuse 用授权 tenant/user/session 组合生成隔离身份；真实 SDK 属性只在 observation 创建时进入独立 scope，退出立即 detach，流 yield 不持有 OTel/DB context。辅助查询模型独立选择、关闭；仅追踪失败可降级，供应商失败和取消照常传播。 |
| 16 / `a2de880b` | Python/Web 统一 ECMAScript trim 与 1–100 Unicode code point 昵称规则，允许字母/数字及有限 ASCII 标点。POST 必填、PATCH 省略保持、null/非法输入在 DML 与密码 IO 前 422；实际浏览器/API/SQL 验证。 |
| 17 / `e3be39d0` | 目录从 530 加 271 到 801 项，完整保留旧条目归一后的字段，不以抽样代替完整 JSON 比对。 |
| 18 / `30724140` | 再加 324、替换 1 项后共 1124 项；旧 glm-4.5v 保留 alias。全键校验、大小写 alias 去重及本地 capability 名映射通过；103 个旧 dimension 转 max_dimension，94 个 dimensions 保留。目录标签不承诺对应推理能力。 |
| 19 / `e96bc37d` | 仅修复活跃 Go admin heartbeat：服务端收到时间和单调时钟判活、30 秒边界、状态恢复及并发快照。NATS、独立 Go ingestion worker、其队列管理和本地 parse/chunk/EXPLAIN **未实现，不能视为等价**；Python 生产 Redis 队列保持。未加入无消费者的依赖、服务或成功占位。 |
| 20 / `22a058f5` | Go graph/file 内部错误返回固定信息，日志保留原始 cause 与无 query 的方法/路径；保留原 HTTP/业务码、公开 404 及鉴权 102 合同。真实 Router/SQL/MinIO 失败路径验证；CI 增加相应必跑身份及缺失/skip 拒绝。 |

## 验证与输入绑定

Python 为生产后端。最终组合在独立、锁文件一致的 Python 3.12.9 环境运行：**639 项定向单测、11 项 HTTP/SQL 集成测试通过，0 fail/error/skip**；全部改动 Python 的 Ruff/format、import-linter 与 async DB 检查通过。run 为 `7fca8ad0df404aa4819709d8b967d651`。集成使用 `REQUIRE_SERVICES=1`、现有隔离 case fixture 和独立提交/失败副作用读回。未运行完整 unit/core/typecheck，也未宣称生产及付费供应商验收完成。Langfuse SDK 为 4.14.2，LiteLLM 为锁定的 1.82.5。P 的历史 327 项执行后发生格式化，原 journal 保留格式化前输入；最终 639 项覆盖当前字节，不将旧证据改标为当前源码。

Go 为停滞的并行实现。I 组合 `967b480c9b1d4045b0e1fffb4b6dc8a7` 完成全部 `internal` build/vet 与三个 main 的独立 native 链接；新构建 native archive 绑定 138 个 C++ 输入、Go 锁、Darwin arm64、Go 1.25.14、Clang、PCRE2 与 SIMDe。字典为借用的未版本化本机资源，只记录运行字节哈希，不将父仓库 HEAD 当字典来源。另有 5 项真实 tokenizer race/native 回归。复用 G 的 292 条含子测试 models race、6 项 entity、10 条含子测试 SQL/HTTP/CLI，及 R 的 heartbeat、handler、真实 SQL/HTTP/MinIO 验证；所有改动路径最终与候选逐字节一致，复用范围见交接与验证索引。未运行完整 Go 默认测试组合。

Web 为独立仓库。最终 `5b577777` 的组合 `f4b8cbbdc196450d9c711a9a62aeb6d9` 完成生产 build、**143 项定向 Vitest（0 fail/error/skip）**、两个修改的 Node API 测试文件、改动 TS 的 ESLint、file-size、i18n-types 与 bundle-size，七步均 raw exit 0。实际 13 个工具/运行包版本与锁核对；使用只读包链接与自有缓存，未宣称重新 npm ci。复用 W/U 对未变源码的浏览器、亮暗/中文/英文/韩语、390px、键盘、DOCX 及实际 Python JWT/SQL/Milvus 消费者证据；受控 keyword/embedding/model 边界不等于外部供应商验收。多张截图按 contact sheet 审阅，W 早期 live 截图的未完成语言资源不作为最终翻译证明。

I 将 Go 组合、Python 组合和最终 Web manifest 与最终已提交源码再次核对；主树原有 WIP 单独核验，不混入干净组合的验收结论。CI 保留最终组合的完整分组及 `ci-required`，补充 expanded catalog 与内部 handler 必跑名称，失败、缺失或 skip 仍拒绝。CI 配置提交不代表远程执行通过。

## 已知失败、退役与范围

I 在相关移植前实际复现并保留两项既有失败：Python OpenRouter credential probe/save/reload 的 `Invalid OpenRouter credential`，以及 Go `TestProviderFixCatalogRoutingAndCapabilities` 的 Replicate rerank catalog 断言。相关基线源码与 `42a54005` 一致；本批未擅自改无关逻辑、削弱断言或宣称全量全绿。此前批次的其他失败不能替代这次现状证据。

2026-11-06 后续已按当前合同修正这两条旧测试并复验，另完成独立 Web fresh install；见[凭证、目录与安装复验](after297-provider-web-followup.md)。后续通过不覆盖本节保留的首次失败、资源记录偏差与未实现能力。

每个 owner 接收后先实际 unbundle/恢复并核验 commit/tree/改动 blob，再精确删除自有 worktree/ref/实际 git admin、临时恢复库、运行环境和缓存；I 最后退役。原始命令、输入哈希、首错、raw exit、JUnit、case 资源独立清理读回与恢复 bundle 留在证据根。主树原 28 项 WIP 的内容、状态及空暂存区均保留，不编辑其脏 `PROGRESS.md`。最终独立读回以 `cleanup.json` 为准。

`strictProtocolPassed=false`：I/B 首次 worktree 的事前登记脚本失败后创建仍继续，R 的首次状态输出早于明确登记，W 首次 hook bootstrap 不可用后补跑相同检查，均保留真实偏差。U 的 npm 命令曾通过借用链接改写主 Web 的 `node_modules/.package-lock.json`；原缓存未留存，无法精确恢复。保留修改后哈希与只读副本，未猜测恢复或删除共享资源；最终 Web 验证使用自有可写元数据。该共享派生缓存的改变不能声称为所有借用资源原样不动，后续 fresh install 的一致性仍待独立验证。

未 push、未部署、远程 `ci-required` 待运行。游标仅推进至 `22a058f56cb39334539eea08ea37e243faf61a5d`，该游标到冻结参考上限还剩 **3097** 个第一父提交；本批不继续下一批。
