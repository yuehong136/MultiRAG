# 历史移植判例

只在当前改动命中类似接口或差异时查阅。这些是历史决策记录；提交状态、代码和验证结果
需当次复核，旧的逐项确认或测试处置不是当前默认流程。当前边界见 [Skill 入口](../SKILL.md)。

| 判例 | 教训 |
|---|---|
| 7827f0fc（mind map fix） | 「真修复逐字搬 / 顺带重构映射我方抽象」二分法的确立 |
| d32967ed（excel LazyImage） | 无抽象分歧的领域 → 行为+改名全忠实照搬 |
| dd839f30（matplotlib/tool-calling） | 文件映射要核实（chat.py 非 chat_model/base.py）；重 service 顶层 import 触发循环导入 → 惰性 import |
| #13741（files RESTful） | **动手前先 grep 目标接口是否已存在**；路由撞车时收编为唯一入口 + 墓碑退役 |
| #13717（b308cd3a，Go token） | 多主题 PR 看 `--stat` 完整足迹；鉴权链最易漏但最关键 |
| #13765（Go admin tokens） | 上游 PR 描述与实际代码不符时以代码为准；我方已修对的 bug（LIST 带 user 过滤）保留不回退 |
| #13776（contextengine） | 先确认上游契约到 HEAD 是否稳定；与我方既有 SQL 命令冲突要消歧；用户要求后可反转「跳过死代码」决定全量对齐 |
| #13831/e20cf397（model pool） | 上游大块 diff 可能是在补我方已完成的东西——只做真正新增 |
| #13887（provider 五表） | 上游「两套并存」可能是多提交迁移的中间态而非我方跟岔——`git ls-tree <commit>` 验证；合并点（#14398）到了再删 |
| #13903（Go upload） | 上游 Go bug（路径解析）→ 对齐我方 Python 语义修掉；照搬保留与顺手修要向用户逐项确认 |
| #13928 + #13706 回补 | 缺基盘的功能先补基盘再开写侧；绝不做假 stub；旧注释变假要顺手修正 |
| #13948/#13952/#13955 | 跳过判例四型：WIP 不编译 / 对我方回退 / 上游已废弃路线 / 半成品 bug |
| #13983（log level CLI） | 「只做本 commit 做的事」判据：它删的我删、它留的我留；Go 侧值级适配（我方 config 更丰富）非漂移债 |
| #13956（version RESTful） | Python 加新留旧（deprecated），Go 直接对齐；ragflow 自身 py/go 分歧则不跟 |
| #13974（Go Delete + 表命令改名） | ① 上游大拆文件（index.go→common/dataset/metadata）：先 `git show pre/post` 逐函数 diff 隔离**真实改动**，再按上游结构**重排我方文件**只套真实改动——保住本地质量改进（路径 helper/注释/typing），git 还能识别 rename；② 上游 rename 半途遗留的死函数副本（旧函数改名后又新建同体函数）→ 验 origin/main HEAD 已消亡即不搬；③ 上游新增却无任何规则引用的 lark 终结符（DATASET_TABLE）不搬（标准 lexer 下反而抢 token）；④ 上游 transformer 把 doc_id 误收进 chunk_ids → 用我方既有 remove_tags 的 FROM 截断惯用法修正（#13903 谱系：bug 不照搬，修法贴我方惯用法） |
| 鉴权统一（_load_user 对齐） | fastapi_login 兜底要重写 `__call__` 整体（不是 get_current_user）；改 `__init__.py` 须重启服务生效 |
| #13972（ES9 dense_vector fields） | **上游过渡期洞**：commit 主目的是终态（HEAD 存活）但携带副作用回归（get_fields 丢 `_score`，Dealer/KG 打分静默归零）、上游 6 周后在 #14970 改调用方绕过而非回修 → 结构照搬 + 最小垫片保我方契约 + **记日落点**（跟到 #14970 删垫片收敛），垫片配钉板测试变异验证防未来"忠实对齐"误删；评估时先查该 commit 到 HEAD 的同文件演进链，"后续修复链"可能改的是调用方 |
