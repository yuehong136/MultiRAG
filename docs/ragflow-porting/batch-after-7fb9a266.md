# 连续批次 after-7fb9a266

范围为 `7fb9a26623b0ed67b20c34d00b037579d71d2cb4` 之后连续 20 个 first-parent 提交，
截至 `b7d88f0b0904e427b5e31cdf0dabbae0f8cd7279`；直接后修检查上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。

## 逐项处置

| 项 / 上游 | 本地处置与质量边界 | 本地提交 |
|---|---|---|
| #1 `2c099bbb` | TSV/不支持格式显示真实错误；混合上传仅接纳服务端确认的已提交文件，失败项重试不重复上传成功项，状态操作使用显式 dataset 路由。Python 在原非零错误合同中增添严格 `data.uploaded` 清单。 | Web `e04a6191`、`8d292eba`；Python `673c8639` |
| #2 `82318dee` | Go connector 创建按租户、名称/source 和 JSON object 校验，拒绝发生在 SQL 写入前。 | `a3671176`、`f087a0e6` |
| #3 `43cbfd44` | ExeSQL 按语句回滚并恢复，连接正确关闭；Trino 使用 AUTOCOMMIT，仅查询错误可继续，传输错误终止。应用直接后修的 SSRF 防护并保留 lazy ODBC。 | `8e7dd4ef`、`5049e0dd` |
| #4 `129e1e31` | 在[HTTP 参考](../references/http_api_reference.md#执行-agent)补普通/OpenAI Agent 的 `chat_template_kwargs` 示例、默认 stream 和模型能力边界；保留本地 object/null、false/0 和每轮隔离合同，不恢复退出的旧路由。 | `efc3c22f` |
| #5 `0071e98c` | 评估不移植 RAGFlow v0.25.6 发布文字。本地无对应 release_notes，不复制未经复测的版本、Recall/F1/性能和法语 keys 数量；可操作的参数文档收益由 #4 承接。 | `efc3c22f` |
| #6 `f0cb7a54` | 完整核对上游 55 路径；保留当前 worker/queue/metrics 架构，接纳 limiter、首轮日志、TOC 源位置与数值排序、合法 filename weight=0、CLI 参数及模型作用域清理。模型仅关闭本任务新建实例，排空 in-flight；清理取消不覆盖已有业务错误。未引入双录制/回放及整套执行器替换，避免新增并行执行状态和全局写拦截成本。 | `5c897812`、`baa8973c`、`be336295` |
| #7 `b472ceeb` | Go 用户设置 PATCH 使用严格对象与字段合同，租户隔离、拒绝零副作用。 | `a3671176`、`f087a0e6` |
| #8 `0940f1a1` | REST 文档/数据集、鉴权、过滤、元数据、原始二进制、并发、批量与真实 CLI/MCP 合同矩阵；显式空数组和非法标量不得扩大过滤。 | A 八提交，末端 `03f0717c` |
| #9 `0aff6a3f` | page_size 原始输入严格验证、上限 100，逐路由保留合法零语义；CLI/MCP/Web 按实际 total 聚合、拒绝缺失/重复 ID、漂移总数与异步旧请求。真实浏览器发现同更新时间跨页重叠后，活动列表补唯一 id 次序。 | `9e229b7b`、`a9e897d7`、`b3594c9e`、`03f0717c`；Web `b8f784bd`、`ab1debfc`；排序 `70aff06e` |
| #10 `c4c4e228` | SharePoint Graph 遍历全部驱动、递归与分页；最后一次 SDK 响应之后再次检查 deadline/cancel，保留 ClientResult 原始字节。Web 配置回显保留凭据和未知字段。 | `309647d2`、`f8e6443d`、`217d9375`；Web `02e69237` |
| #11 `085241b0` | Go healthz 实际探测依赖；Infinity 不支持该接口时明确失败；S3 使用只读 HEAD 和配置 bucket 回退，不伪造健康。 | `a3671176`、`f087a0e6` |
| #12 `0694b4af` | Go 受管租户模型设置 PATCH，适配字段、模型归属解析和权限；活动 GET/PATCH 路径为 `/api/v1/users/me/models`。 | `a3671176`、`f087a0e6` |
| #13 `8468227a` | 302AI 规范化 key/header、按调用方法固定 stream 并忽略冲突配置、严格 inventory/vector/stream 响应；已有 answer 后收到 malformed null choice/delta 也必须失败，不能伪 DONE。 | `b2274050`、`7943efd7` |
| #14 `7e836435` | RAPTOR 使用 canonical clustering 字段和默认值，false/未知扩展保持，AHC→GMM 实际保存与重载。 | Web `e04a6191` |
| #15 `5de021eb` | Slack 旧父消息仍读取窗口内新回复，显式 `[]` 表示无来源；最后响应停止边界与权限/限流错误严格传播。两源非权威完整库存均不支持删除同步，Web 隐藏不支持测试/删除同步操作。 | C 三提交；Web `02e69237` |
| #16 `bea80920` | 开发启动命令用已实际测试的 `-i 1`（`--index` 同义，默认 0），仅 `-t common`；唯一容器调用同步迁移，不承诺不存在的任务类型队列隔离。 | `5c897812`、`83b49326` |
| #17 `32d5bf97` | Go MCP 创建严格字段/租户合同，持久化与拒绝副作用读回。 | `a3671176`、`f087a0e6` |
| #18 `ed878930` | Go connector 生命周期日志与 KB/binding 授权保持一致；存储不确定时保留 SQL 可重试状态。 | `a3671176`、`f087a0e6` |
| #19 `ff9aa4e2` | LongCat 等共享 HTTP transport 保留自定义 RoundTripper、连接池和实例隔离，流式 idle timeout、取消与错误可验证。 | `b2274050` |
| #20 `b7d88f0b` | Voyage 本地共享 transport 已等价覆盖上游；补严格向量、维度、索引重建、默认 topK 和实例隔离验证，不引入重复实现。 | `b2274050` |

## 验证与复用

冻结组合与原始报告在本机验证目录 `20261025-batch-after-7fb9a266`，按 `scope.json`、
`integration/accepted-{A,B,C,D,G,W}.json`、组合报告及 `delivery-complete.json` 逐层绑定。
单线报告只复用于源码、配置、锁文件、后端和消费者输入一致的覆盖面；新增修复另有定向证据，
没有把旧提交通过数称为新提交全量通过。

- Python：`70aff06ebb9980c337b1643195e480154de9935c`，四个定向 integration 文件 39 项通过，零失败/错误/跳过；`I/python-combination-01/junit.xml`、外层 `d812b9a504f5483da697ce305776f901` 原始退出码 0。真实 PostgreSQL/Redis/MinIO/Milvus，隔离资源，`REQUIRE_SERVICES=1`。排序补修另有 72 项真实 SQL 和 3 项相关单测。
- Go：`c649e764` 上构建、vet、三个入口重新 external link 和 help，race 42 个顶层用例、360 项含子用例，零失败/跳过。在最终 `70aff06e` 上逐文件复核 3,471 个 Go/配置/锁文件/Web 输入与原执行一致，复用报告 `I/Go-reuse-against-70aff06e.json`，保留原 Go 执行 SHA。Darwin arm64 native 库的 135 源文件、24 字典及工具链/hash 与既有证据一致；本次重新链接，不声称 Linux 构建。
- Web：master 和后端 consumer pin 均为 `8d292eba72b0af4409755d504c12947ca8522951`；真实 Chrome + 实际隔离 Python HTTP 的两源 CRUD、RAPTOR、严格分页、混合上传、失败项重试和状态更新，独立 SQL/对象 hash 读回。排序补修后的真实选择器为 `100/100/5` 三页、205 个唯一 ID，第 205 项实际选中；独立 SQL 有序 ID 清单全部匹配，4 个外租户项排除（`I/selector-final-independent-SQL.json`）。状态操作有实际点击/HTTP/GET 与 SQL 证据，没有单独状态截图。
- worker/parser/供应商使用受控边界；真实 SQL、对象、队列、索引及当前 `handle_task` 不等于真实供应商调用或常驻 daemon 验收。Trino 驱动未安装/锁定，当前仅受控驱动合同；没有真实 SharePoint/Slack 权限或供应商可用性证明。

## 资源与证据边界

统一入口见[事前登记验证命令](../development.md#事前登记验证命令)。
门禁随批次修正为helper import 前设置 sys 禁写字节码（解释器启动阶段使用 `-B`/环境变量）、Go telemetry 写入自有目录、POSIX 子进程登记与信号/进程组清理、
只读缓存和 symlink 安全、保留动态 case 收据；`6cd04347` 的 49 项及 3 subtests 仅复用于后续未改动的机制，
新增字节码/telemetry 行为有独立实际探针。

历史 strict evidence 为 false，持续保留：首轮 Python 3.9 bootstrap、早期被清除的嵌套 raw 收据、
未精确登记的早期缓存/模型路径、旧 MinerULocal 监听器、共享 Go telemetry 和早期可变验证脚本/未 hash 配置。
真实浏览器的旧鉴权 fixture、分页重叠和关闭连接脚本失败也保留原退出码；后续通过不倒填旧记录。
无法恢复的早期身份不声称精确事前登记。当前全局独立读回 `I/final-independent-exact-readback-04.json` 核对 3,791 份原始 journal：169 个临时库、146 个桶、227 个 Redis 队列或键、292 个集合、1,207 个自有监听地址、817 个路径、849 个原进程身份与 348 个进程组均已不存在，8 个本批普通 worktree/分支/登记已退休并保留可恢复 bundle。自有服务配置副本随后独立删除，原始配置 hash 未变。前三轮最终读回脚本的类型解析/旧 preview 字段/借用 PostgreSQL 端口归类失败保留原 raw1；修正只解释原始资源身份，9 份旧 preview 的 run UUID 字段均可由同一不可变收据中的真实 child OS 组印证，未重写旧历史。借用服务、
模型/字典/工具链、历史非本批 worktree 与原有 28 项 WIP 保留。

未 push、未部署；远程 `ci-required` 待运行。局部组合验收不称完整 CI、生产、Linux native 或真实供应商通过。
