# 1e80419c 后二十项本地交付

2026-10-28，冻结 first-parent 范围 `1e80419c21117dacc3dbc369d5989443e8605bf2`（排除）至
`33ef724b5f050cb5ee5ca254ac290725f3e3a842`（包含），直接后修上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`；未 fetch 扩大或启动后批。
后端基线 `ebaa10d506fc44ac479896414f84e60dd5676767`；Web 实际派发基线
`e3f50fca8e35c881bdc3334885480abacd7ddcf1`，保留初始 `91877536` 的模型命名规则、
`614782d9` 模型选择器及 `e3f50fca` 能力图标改版。初始快照与实际派发基线分别记录。

## 逐项处置与证据

冻结范围、逐文件补丁及 hash、活动消费者、实现质量比较、候选到主干 patch-id 映射，见
外部证据根 `~/.local/state/multirag/validation/20261028-batch-after-1e80419c/` 的
`scope.json`、`handoffs/{I,M,A,F,P,D,G,W}.json`、`integration/accepted.json` 与最终收据。
`scope.json` SHA256 为 `b5091d2e4183c71ab5948e02c8f191a386ea95a221b69ed09a3cf59ada27d47c`。
每项评估覆盖全部上游路径，并比较失败语义、消费者和维护成本；不为已有等价行为重复造实现。

| # | 上游 | Owner | 本地结果与质量理由 |
|---|---|---|---|
| 1 | `f194e8b4` | W | 本地模型开启 vision 保留 chat+image2text，字符串/数组去重；保留更强的域缓存失效，新增/删除与账号切换实际验证。 |
| 2 | `b35266e9` | F | 严格 get_bytes，只有 None 才回退；零字节可下载，确认缺失 404，不确定存储错误 503，拒绝无未授权读写。 |
| 3 | `252cc19f` | F | 保留已有真实字节签名/PIL 各帧校验与有效成员/注册图片鉴权，比依赖后缀与裸桶读取更强。 |
| 4 | `1a6df01b` | M | embedding 错误类型化、脱敏与可重试分类；保留索引重建、维度/有限性、取消和超时；真实 LLMBundle 到两类检索路由及无副作用读回。 |
| 5 | `fb3bd3de` | P | 英文 Fig./Figure/Table 编号图注，保留中文、布局标签及词边界；实际表格消费者保留独立 caption。 |
| 6 | `f0e4f2d5` | D | 复用集中 SDK BaseURL 与实例独立 map；补 driver 入口非法地址拒绝，五类 SDK 调用及并发实例走实际自定义端点。 |
| 7 | `d398d617` | P | MinerU 跳过页眉/页脚/页码和未知/缺 type 块；避免正文重复和首块异常，保留 VLM 与坐标/tuple 合同。 |
| 8 | `c2597f13` | I | RSS 本地配置与启动说明；batch_size 是批产出而非每轮上限，保留 opt-in prune、当前 feed 镜像与完整性失败边界。 |
| 9 | `3aea80f5` | I | S3 类型模型与实际 Docker 模板使用 region_name；保留 OSS region、额外键及既有 endpoint/signature/addressing 链，补配置与迁移边界。 |
| 10 | `17487239` | G | 新增有效 owner 管理列表，SQL 稳定排序分页、严格过滤与 []；不选择凭据列，tools 固定空对象，嵌套凭据红绿回归后 SQL 原值未变。 |
| 11 | `1696d4ea` | G | 授权码协议采用强制 Redis 原子 state、cookie/通道绑定、PKCE 与 OIDC RS256 claims；始终身份绑定未启用，无 email 自动关联、账号/token 写入或密码重置。 |
| 12 | `eaa19bdb` | A | 非空保存模型保留，空模型才使用租户默认数值实例 ID；direct/override 使用真实 transient Dialog，修实际 beartype 失败且不突变 SQL 保存对象。 |
| 13 | `bebf6ed2` | M | LiteLLM 过滤内部配置元数据，复用原白名单并保留工具、响应格式、推理、MiniMax extra_query 与合法 0。 |
| 14 | `e1403171` | A | REST/OpenAI 流与非流仅在输出边界归一 NaN/Inf 和 NumPy 标量；严格 JSON、引用和完成终态保持，持久化源不改写。 |
| 15 | `67a3ed75` | W | 保留 schema 类型解码，补数字 list enum 与 time 往返；非法/空白/非有限数字和 fractional integer 拒绝，编辑与最终保存均阻止 HTTP，合法 0 可恢复。 |
| 16 | `09d0a174` | A | 保留更严格 text 数组合同；畸形值/混合图片拒绝不静默丢失，原版 OpenAI SDK 真实 HTTP/SQL 验证。 |
| 17 | `088d8448` | M | 现行 SQLAlchemy 已绑定参数且保留调用方事务；真 PostgreSQL 验证引号/注入字面值、幂等和失败回滚，不复活旧 mysql 脚本。 |
| 18 | `a4bc066f` | F | 内部 id2image 按首个分隔保留不透明 key 与合法尾部连字符；当前无仓内活动调用，交付 helper 合同验证，不冒称 worker 路径变化。 |
| 19 | `0f6f7b3c` | F | 受保护图片首分隔、fresh membership 与注册对象约束已有更强实现；新增尾部连字符回归，保留原路径。 |
| 20 | `33ef724b` | W | 无活动 globalfiles 页面/客户端；现行 dataset Documents 与 File ID 合同不同，不制造页面、链接按钮或 legacy alias。 |

实际本地行为见 [RSS 连接器](../../common/data_source/README.md)、
[S3 兼容存储](../s3-storage.md)及[移植映射](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)。
Go 仍是停滞的并行实现，没有把生产 Python 服务迁到 Go。

## 适用验证与复用

- Python 定向组合 **886 passed，零 skip/fail/error**；后续公共 consumer/CI 范围单测另有
  **35 passed**。修改文件 Ruff check/format、mypy **156** 源文件、8 条 import contracts 和
  async DB 门禁均有 raw0。生产源与单测输入冻结后，后续只增加集成用例、Go 和交付索引，
  不用旧通过数替代新输入；冻结与命令索引见 `integration/python-results.json`。
- 当前组合的实际 HTTP/SQL/存储与固定 Web 消费者 **38 passed，零 skip/fail/error**，外层
  raw0。包含实际 LLMBundle embedding 失败、保存/默认模型、原版 OpenAI SDK、图片/文件
  下载、迁移事务，以及真实 Web model/metadata domain clients；失败请求独立读回 SQL、
  MinIO、Milvus。数值 wire 用例控制 orchestration 输出，不能冒称检索质量验收。
- Go 当前组合 `build ./internal/...`、`vet ./internal/...` 和三个独立 main 的 external
  native link 各 raw0。目标 race 按包覆盖 100 个父/子通过事件：Google 83、OAuth 14、
  新路由 1、MCP handler 2。首轮跨包 `^TestGoogle` 误收两个旧 live 用例，产生一个失败和
  一个 skip，外层 raw1；原样保留。models/OAuth/router 包 raw0 复用相同冻结输入，handler
  正确限定 MCP 后独立复跑 raw0，未放宽 skip/环境门禁，首轮不称组合全绿。
- 原生准备只复用与当前 C++/binding/build 输入一致的 macOS arm64 archive、24 个字典和
  对应依赖；新 Go 编译/链接/race 消费当前 mod/sum。不是本批 native 重建或 Linux 构建。
- Web 接受并验证 `5bf3a45998ea19f9e669ae1509095e1a3b21ca64`，consumer pin 固定该值。
  此版本包含独立 `a7b7960a` provider 图标/别名输入，额外完成 39 个受影响 UI 测试、11 个
  metadata lib/API 测试及 production build；明暗浏览器验证新增/删除双能力、schema
  保存/重开、非法值零 PUT 与 0 恢复。22 格 contact sheet 均标文件、页面、路由和视口。
  浏览器使用受控 HTTP，真实后端 domain client 是前述独立的 38 项层。

Web 后续外部 master `e5a2270455620c7388d0cb313dc8d45bfa29706d` 及并发的模型选择器
WIP 保留；已验 `5bf3a459` 为其祖先。没有盲目将 pin 追到该新版本，也不把新 WIP 说成
本批验证输入或字节冻结。前端提交/消费者与 Python、Go 证据分别记录。

## 首次失败、资源与边界

全部候选、I 审阅补修、主干映射及 patch-id 见外部 `integration/accepted.json`、
`integration/dispositions.json`。20 项共 61 个上游路径均有处置及实现质量比较。
保留各 owner 的语义红、导入/缓存、fixture 与命令失败及修复复跑；I 首次单测误选完整旧
MinerU VLM 文件后取消 raw143，随后收窄到原先审结的六个纯用例，886 项通过。无法倒证该
旧 fixture 从未进入，也不补造 port0 事前登记。Go 首轮筛选失败和缺输入的登记失败另存。

I 在新的只读进程中独立查询同一明确 TCP PostgreSQL、MinIO、Milvus 和 Redis，核对
24 个 scratch 数据库、108 个桶、30 个集合、44 个精确 Redis key、246 个监听地址、
274 个运行进程组/临时路径及 7 个浏览器 PID 均已不存在，预留 OAuth Redis DB15 为空。
两个事前探测为借用的监听候选保留；未按前缀删除资源。详情见
`integration/resource-inventory.json` 和 `resource-audit.json`。

初始 28 项后端 WIP 保留；common/app_config 的既有 Channel hunk 与 PROGRESS 的旧 hunk
不入本批提交。其他候选树/ref/admin、自有 Go 缓存、模型软链、私有临时文件已按登记精确
退休；I 最后自身树与恢复探针、bundle hash/恢复读回及 main 最终元数据提交由
`delivery-complete.json` 封存。借用服务、模型/字典/模块缓存原件及其他历史工作树保留。
历史严格事前登记结论仍为 false；资源最终缺失不补成“从未有登记缺口”。

本批仅本地 commit，未 push、未部署；远程 `ci-required` 待运行。未验证真实供应商、
真实 Tigris、OAuth 实际同意/浏览器 cookie/平台账号登录、常驻 worker、生产迁移、Linux
native 重建、Go 整应用 MySQL 启动及干净环境重装。未启动后批或自动化。
