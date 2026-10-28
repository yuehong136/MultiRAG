# 33ef724b 之后二十项

本批冻结范围排除 `33ef724b5f050cb5ee5ca254ac290725f3e3a842`，至
`ff5971448b8ea2bead108732f55e20c3e9a42679`，按 first-parent 连续二十项，
共 96 条逐项路径足迹。直接后修参考上限为
`519e7d98a5651564d4e35d6648f006cba4baaf4f`；未 fetch 或扩大上限。

后端起点 `acf4669b18a979b4ae1f71658068349917a24df4`。Web 最初快照为
`123f4a14eda8627e1530aa45d34eb8735030ccbb`，实际派发起点为
`6abc8e26ac2befa42a97616623e145a0ad4fa391`；保留两者的选择器身份、库存失败状态、
搜索与多能力筛选改动。前后端分别提交，只做本地串行集成，不 push、不部署。

## 逐项处置

| # | 上游 | 处置与质量比较 |
|---|---|---|
| 1 | `2e02bf7b` | 保留数值模型 ID、租户/能力解析和已有两段/三段名称解析。逐核十三类配置；全局模板无租户、历史快照和外部连接配置不做盲目递归改写。补启动默认值的 OCR 关联，只填非空名称且数值 ID 缺失的字段，保留已有选择；500 租户一批、条件更新、歧义回滚。 |
| 2 | `5b02fe48` | 生产流已经逐 delta 输出且终帧不重播答案，保留其更强的最终单帧、错误、严格 JSON 与 Unicode 合同。补真实 OpenAI SDK/TCP 消费者；旧 final-answer 测试替身改挂 detached model 的实际构造入口，不削弱断言。 |
| 3 | `600590cd` | 在活动 `core/llm/cv.py` 的 Qwen3.x 图片与视频调用关闭 thinking，覆盖 3.5/3.6；不修改停用副本。 |
| 4 | `e4ef9834` | MiniMax 已统一支持 thinking 的 true/false/未传语义，普通和流式路径共用严格构造，保留现状。 |
| 5 | `d4f1c2c9` | 本地无重复 transport helper 声明，保留分拆测试组织，不创建无消费者文件。 |
| 6 | `0e9eeb7b` | Go 增加 metadata/config GET/PUT，保留 Python/Web 的 schema、number/list、合法零及省略/清空语义。锁定并合并 parser 配置，用 UseNumber 避免大整数损失；独立 SQL 验证权限与失败无写入。 |
| 7 | `3bc5ed28` | 采纳 provider 实测验证和有效状态语义；保留 TEXT credential、租户/provider 和 provider/instance 唯一性，以及 JSON 多能力整合。旧 MySQL 工具由 Alembic/SQLAlchemy 替代，不复活旧表与字符串身份。Go entity 次级足迹有独立回执。 |
| 8 | `1092f624` | 主 token/beta 已独立随机生成并有 CAS 补填，保留现有协议；不将旧存储方式冒称哈希存储。 |
| 9 | `5733e062` | N1N 共享 transport 已保留自定义包装器、nil 防御和实例隔离，较裸 fallback 更完整；加精确 witness，不退回弱 transport。 |
| 10 | `4018f02d` | Alembic 版本已提供事务内一次性迁移，后修 `76fc1d54` 也改为数据库版本链。本地不叠加会漏后续遗留行的全局完成标记；关联回填改用 SQL NOT EXISTS，只取未关联行。真实 SQL 验证版本与数据共同提交/回滚及重复升级。 |
| 11 | `2819d0ea` | Go 普通 2 分钟、流式/长媒体操作 10 分钟，复制 APIConfig 并保留父取消；取消活到响应体和 sender 结束。覆盖实际拆分媒体 helper，保留连接池及 idle 限制。Web 次级仅排版，保留现行 locale。 |
| 12 | `a98889cd` | Go MCP update 保留稳定 ID、所有权、描述 null/省略语义及安全投影；不 SELECT 或回显凭据，并避免 SQL 日志泄漏。并发重名和失败事务有真实读回。admin/RSS/OAuth 次级仅空白，不复制无本地入口。 |
| 13 | `c990badd` | 更新 MiniMax M3 目录，保留现有生成参数与多能力约定；目录存在不代表真实供应商可用。 |
| 14 | `d41373cf` | 更新 Anthropic/Voyage 目录；Python 活动 chat/CV 和 Go Anthropic/Bedrock 按真实模型身份处理采样参数，兼容前缀/地域名，保留较早模型允许的参数组合。 |
| 15 | `36357a6a` | 创建服务端始终验证一个代表模型，再原子保存 provider/instance/声明；无写入连接测试复用 typed candidate，不持 SQL 写锁跨外呼。SiliconFlow 使用独立 region 和 raw key，历史 source_fid 仅窄只读适配。Web 同步验证/保存、合法零和库存刷新；活动 CI 消费脚本同步原子合同。 |
| 16 | `9f969feb` | Go 无写入地域/key/baseURL 探测，严格区分缺少 key 与合法空 key；未知地域和越权零外呼，失败脱敏。 |
| 17 | `5f892641` | Go 注册 connector PATCH，区分省略/null/false/空对象；配置、关联检查、排程取消/重建同事务，行锁串行并发，失败独立读回。 |
| 18 | `9799f335` | CLI provider+region+key+URL 贯穿真实 Execute；严格语法、限长响应、安全错误和非零退出。KEY 命令不落历史，保留旧历史；新构建 CLI 经过真实 JWT/Go 路由/SQL/受控厂商链。 |
| 19 | `583daf47` | provider/model 库存稳定排序，保留未知厂商和现有注册能力；目录清单不引入无驱动的内部厂商。 |
| 20 | `ff597144` | 短 Markdown 标题与下文合并；仅完整的短 ATX 标题组可触发例外，正文出现即恢复阈值，避免上游多行误判。真实 Markdown/tokenizer 覆盖位置、图片顺序、overlap、RTL 与空内容。 |

## 模型迁移边界

上游旧工具的十三类配置在本地分别落到 tenant、knowledgebase、document、dialog、
memory、search、canvas/template/version、API conversation、pipeline log、connector 和
evaluation snapshot。活动调用通过租户内数值身份或明确名称解析；模板在执行时绑定租户，
历史记录保留原始内容。配置中出现 `llm_id` 等键名并不构成跨租户改写依据。

schema 由 `usr_ai.alembic_version` 管理；模型注册关联和新导入行的幂等补填是不同工作。
默认值修复 helper 由调用方事务管理，启动阶段提交完整租户修复后才接后续遗留初始化。
其他初始化 service 的既有提交边界未改，不能把全部启动过程称为原子迁移。

## 验证与证据边界

完整候选到主干映射、输入 hash、原始 exit/JUnit、首次失败、最小复跑、逐路径处置和
精确资源读回保存在本机外部目录
`~/.local/state/multirag/validation/20261028-batch-after-33ef724b/`。
最终交付以该目录 `delivery-complete.json` 的实际 main、Web pin 和组合报告为准。

远程 `ci-required` 因未 push 待运行。受控 HTTP/SQL、浏览器、供应商真实账户、常驻
worker、部署和生产是不同层次；本批不宣称真实付费厂商、生产或干净重装验收。
Go 静态库复用有明确源码/构建输入/hash 边界，实际重新链接三个入口，不称 native 重建。

最终可执行后端组合为 `761af5a277dc53ea7d3d8f1fbd971e5a6e85bd84`，后续
`dd447b5e8ca083161300c2783b17ff5b0a5c90dc` 只调整消费者测试凭据的 JSON 空白格式，
保留精确 SQL 字节断言。Web master 与 consumer pin 均为
`98712181c9c7bf8ccf39c955d8bbab467fd77946`。

本批适用的定向组合结果：

- Python 单测 220 项、补充 provider schema 合同 23 项通过，零 skip；改动文件 Ruff、
  format、当前纳管 mypy、import contracts 和 async DB 检查均 exit 0。
- SQL/HTTP 首轮 29 项通过，Web 精确读回用例因旧夹具 JSON 空白失败。保留原始失败，
  改为 UI builder 实际提交的规范 JSON 格式后，仅复跑该用例，owner/foreign/cleanup
  全部通过；不是声称首轮全绿。两轮合计覆盖 30 个不同用例，零 skip。
- Go 当前源码 `build ./internal/...`、`vet ./internal/...` 与三个独立入口的实际 CGO/native
  链接通过。模型包 race 为 462 个顶层测试、3583 个含子测试的通过事件；CLI 定向 8 项、
  管理 SQL/HTTP/新构建 CLI race 10 个含子测试事件通过，零 fail/skip。
  历史模型测试的 listener/temp fixture 通过登记的 Go overlay 接到已有资源门禁，
  未改业务源、断言或 skip；原始 overlay 和 hash 保留。
- Web 产品源的 307 项 Node、57 项 Vitest、build、lint/文件大小/i18n/库存门禁和
  明暗主题受控浏览器证据来自其固定候选。最终补充的现用 CI 消费脚本单独通过
  strict type/lint，并在上述真实 Python HTTP/SQL 组合执行；产品源与已验候选逐路径相同，
  不重复构建。截图先按文件名、路由和视口组成 contact sheet 审阅。

候选 bundle 均实际 verify，并在隔离对象库恢复读回；薄包的精确基线边界保留。
原 28 项后端 WIP 保留，Web 接收前后干净。最终精确 tree/ref/admin、scratch DB、bucket、
collection、Redis queue、listener、进程及私有缓存清理，以外部交付记录的独立读回为准。
