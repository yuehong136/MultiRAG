# 5d28bb07 后续 20 项组合交付

本批 ID 为 `after-5d28bb07-20`，从上游游标
`5d28bb0701da9f248deabc183e41aa82bf5e638b` 之后连续 20 项，
末项 `94f82acd03a0ae82eeaae3cede57d319d9a5f792`；冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。游标不是 MultiRAG 提交。
本批初始后端 `db517cd394b33774bf1d1b49f0dc8560303eb00a`、
Web `6ad9577e3d76bdd91d2df9915b6e0c21e447f5ac`。
本批20/20项已纳入本地提交组合（包含经验证的既有等价项）。
旧批报告与旧验证数字不作为本批通过证据。

## 上游、候选与实际主干映射

实现合同由对应专题维护；本报告只保存范围、集成映射、组合证据和边界。
候选逐项审查后串行落到实际 main/master；无临时缺授权的文档迁移版本发布。

| 编号 | 上游 | 候选 | 实际主干 | 处置 |
| --- | --- | --- | --- | --- |
| 1 | `2fd8cdc3cc40b28ebd6ff578b9ec095c5cdce8ad` | `ce7f53a3417e5c4566fabe7d49d0949153d949eb` | `04f4183af30fccbaa47dec996261f8c1d0905082` | 连接检查与发现；见 [provider discovery](provider-discovery-openrouter.md)。 |
| 2 | `a86e0ca0ca2037ec97b6585b47b7ee92f1259519` | `806c22653367cb4bb9496b7860d72907b5bed272` | `cf01ac84afd6d6833573965169a6106736708804` | 余额；见 [余额与向量合同](provider-account-and-vector-contracts.md)。 |
| 3 | `a377512110c912990973ba27d44e7389544b25f6` | `e287ae51059119e2e33540b3fc7d86ec60d9de2e` | `f486d64bb314a78e4e55150e219abd2ed71af86f` | OpenRouter 基础及完整附带足迹。 |
| 4 | `e729eced45968e2818fa16ed03efee0eed305942` | `011880427e5bbe3f9d67a03d62c77f44260f903a` | `7617e36a6e217558e7bcf683a551a0851ac329e6` | 余额；见 [余额与向量合同](provider-account-and-vector-contracts.md)。 |
| 5 | `c7ddc8c0391c6f634aff9c087d2c7888b7a3c8af` | `c94754f0ccb1395b6ea287db33764a3a84ce4b99` | `e27fb2d206772bf8841e85c8885d21b0f190ffed` | NVIDIA 发现与连接检查。 |
| 6 | `f063e03a245540ec01800fe2e8a7f7dc489826d2` | 无新增提交 | `6b12e486044c9af1a32eda6fecddf51eb488d670`、`02bf325667cc796a43b2654a2a485b1dd18e8c29` | 已有 bucket 隔离和有效 SPN SDK 调用；本次受控 SDK 复验。 |
| 7 | `d8d49df35e8168f78e2af9050afa247c91aa45ce` | `16ab74457cbc14ba6d60177c2095a31c4f8d7ff5` | `b93f12fcf7453c4cdb3fbe0438cdca31022fd8e9` | Gitee 严格索引重排。 |
| 8 | `412fae7ac2380db6afa751c50c32625b01a34e6e` | `645e59fe7b0fd8f64fecf53eed4a7142632861ee` | `df766a570626b2048d94a8177cece45f7a7a2a05` | 已解析类型诊断；见 [MODEL_CONFIG_DIAGNOSTICS](MODEL_CONFIG_DIAGNOSTICS.md)。 |
| 9 | `f703169117687dec3a6550fe21c8ea20c6497e70` | `97d35e855b3123e0f7ff0c7645eb6949143d5421` | `44b8b9f9586646fd7eb9d7fc89c954a62f69b602` | 9→14→16 作为完整授权链接受；配对 Web。 |
| 10 | `65477519366adc9dff1368aa8dc368889703d659` | `8266cef0dc9d8c790d4628da145489469950caa8` | `761e69717d6c3f46e0384b32b8d3c3074f7ec80a` | 生产已有等价检查；新增后台转换前拒绝零副作用证据及 `c4b2f8cc60dbcc0c1eaf025ec5ec6b93e0690b72` 旁路任务保全回归。 |
| 11 | `731c887ba070ce47385255e03fe895e427702453` | `137b2885b3f04403804ffd1330b92fbf980d52cc` | `5a402078d6393bf713c3909c9be4c836e750afa1` → `9ce3c058390514f64191bee346853aac47de77f8` | CLI 与路由迁移；19 同链及最终 `d08c8cae12be099348162ee6bda7d52e0904f104` Bearer 修复一并覆盖。 |
| 12 | `d13a240dc0b1f2a3ea53d59dfe31a5c625cb3434` | `63d498f75c31724df2b6c25c1663dc1251270d29` | `566493683aae8c309e2d965ab4c40f41a279b7b8` | OpenRouter 余下接口及旧 wrapper 退役。 |
| 13 | `a82ae4a991d1fd535f41b8eb5d2710e06458ecf5` | `7d32175059d4a09e3d8462045ae38a29b259936a` | `6ee25285bd9811bd73b3294a3e30bce3cbad08a9` | Aliyun 复用既有严格 embedding 管线。 |
| 14 | `d51fb88573861e68f40d795be93101f25567b8dd` | `3f7ddf46636d6b0341fa465e844ba47b089788a7` | `fec1ab18da646f9a7b578527555f384754e974f2` | 文档 tenant 与存储来源；保留本地 owner/admin 管理下载边界。 |
| 15 | `1bcb6deb6f45d97370c23b0713a258bbf71ce50b` | `b56d67dd31be72be411c6f7e985344b3aaf1cee0` | `4bc9dbe0fd1ada86c545460bfe5eae98f90e320f` | 后端/Web 成对；组合 Web 回归修复另列。 |
| 16 | `d843035c8bbb9f370de2052e7aeaaf151b02833f` | `289ac141bdb16b0d884eac4fe3f6947b361df14d` | `b3c76f5a3c1c826fc41c833f44d9e2ab4dc1f53e` | 显式 /v1 生成附件兼容承诺；同一授权 handler。 |
| 17 | `decb5dcb6f25a2be7d92d33277edc444d2cf961b` | `e6b2bfdac5c614f1bc0a0d2fa73a4a37804091e5` | `641fb263c104dc8a3e7f9a70e163d4ee8f7ccfa4` | Canvas path reset 与暂停表单聚合适配。 |
| 18 | `69197d4a8f993ecc7308f8d75258139fc1ea3709` | 无新增提交 | 基线 `db517cd394b33774bf1d1b49f0dc8560303eb00a`（接受时 `566493683aae8c309e2d965ab4c40f41a279b7b8`） | int|null 已等价；实际 HTTP/SQL/SDK/Web 类型证据。 |
| 19 | `ee5ae6f1a430677b1277a603d55d5097348ce525` | `b05b84fa09ce1713475cd32c96fbd4ca128b6b45` | `9ce3c058390514f64191bee346853aac47de77f8` | 注册、密码互通与 logout，组合 follow-up `d08c8cae12be099348162ee6bda7d52e0904f104`；见 [CLI_AUTH](CLI_AUTH.md)。 |
| 20 | `94f82acd03a0ae82eeaae3cede57d319d9a5f792` | `6965316fc8fd4c33f1c256c167ca294ecb740d72` | `bae9611c27644c26393288da8fe3a7073e9334f0` | A provider/service 与 C CLI 足迹合并计一项。 |

A1/3 稳定基础先交给 B；A5/12 与独立线继续并行，B/C 真正合入后 A20
从 `9ce3c058390514f64191bee346853aac47de77f8` 接续。第 20 项的现有
实例克隆与 CLI formatter 按真实机制接受等价，Ark embedding/catalog 与
SiliconFlow 空配置防护的实际缺口由 A 补齐，没有重复计成第 21 项。

Web：F 候选 `138348551658f07a0e259548ee0d4c1e63464e08` →
实际 master `5b94815692bba9cdc42aec96225569806547e06c`；
H 候选 `2f8bbef25d40a6ce373f55d80ef68a32f38a53ae` →
实际 master `1cc51c4165a012d1ae3753b04c65d433ca782979`。
思考 UI 组合收尾为 `a196a95d8b3b0f9238ba834ce273704a1f270bcc` 与
`e6483235657b6b49757fc73e8430df50305c7451`，不删除原有断言：
reducer 恢复无思考时保留空格、有思考时裁剪的既有 runtime 合同；
Studio 测试验证新要求的默认折叠、展开后正文完整及流结束保留用户选择。

文档下载组合回归 `6d9e524bebbc092f9b1b95c0699e2f92ec39c163`
使成功下载保持二进制 Response，避免 JSON 文件被通用 API 解码吞掉；即使文件内容为
`{"code":109,...}` 也按原字节下载，非 200 错误仍拒绝。
Web `53fdf0968b901daaa7d54ea6db911c409b049eaa` 和后端
`dd4e1e019c70ae0a366339884db9d94ce3b2a95a` 增加真实 HTTP 消费者验收：
普通文档/生成附件各覆盖 JSON、空字节，独立读回对象并比较 SHA/长度，匿名拒绝和
SQL/存储前后不变。原预览/下载断言保留；定向合计 19 passed。

真实 Python API/CLI smoke 发现用户 logout 发送裸 token 后返回401。
组合修复 `d08c8cae12be099348162ee6bda7d52e0904f104` 只为用户 logout
补齐 Bearer scheme（已有前缀不重复），admin 仍按原 GET/raw token 合同。
保留原错误业务码与会话保全断言，补齐两种 token/两种响应 envelope 和 admin
回归；红测、修后完整 CLI race、完整 Go 门禁与实际 API/CLI 复验均保存。
这是11/19同链组合修复，不另计为第21项。

集成期间实际 main 新增独立 CI 提交 `a2793bedc074923af811ad11b8dd71e8ce013a7d`；
本批保留该提交，只重放当时尚未接受的 C 候选。消费者 pin 随最终 Web
更新，版本唯一来源为 `.github/consumer-web.json`；本批未 push，远程 CI 未执行。

## 组合验证

本轮最终执行输入为后端 `c4b2f8cc60dbcc0c1eaf025ec5ec6b93e0690b72`、
Web `53fdf0968b901daaa7d54ea6db911c409b049eaa`。
集成工作树无 tracked diff；2927 后端与 2296 Web 文件的 SHA256 清单
见 `integration/final-source.json`，组合 digest
`b47e3010c74ad8456368c99fe184ba35b164c87ca3b72d04ad9a860ec270ed5c`。
报告与索引的后续文档提交另行记录，不改变这些执行输入。

执行环境为本机 macOS/arm64，专用 Python 3.12.9 frozen venv、Go 1.25.14、
Node 24.4.1；Docker 服务镜像/ID/架构及运行时版本见
`integration/final-runtime-services.json`、`integration/private-runtime.json`。
本批配置独立，MinIO overlay 修正不落共享配置；core Redis DB1，跨语言 smoke 另用
事先确认空的DB14。native 静态库 SHA、rag/opencc/wordnet 字典清单另保存，未替换共享资产。

原始证据根目录（本机持久目录，未入库）：
`/Users/xldu/.local/state/multirag/validation/20261018-batch-after-5d28bb07`。
下表路径相对该目录；执行 receipt 保存命令、起止 SHA、退出码和时间，integration
报告另保存 collection/selected/excluded、JUnit、逐项事件和 fixture 资源读回。

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| `make verify` | exit 0；6600 unit passed；Ruff 1710 文件、8 import contracts、async DB、mypy 147 文件通过 | `integration/verify-final-r4.{json,log}` |
| 完整默认 core `REQUIRE_SERVICES=1 make integration` | exit 0；1127 passed、19 external consumer deselected、零 skip；实际 workers=0 | `integration/core-final-r4.{json,log}`、`integration/core-final-report/` |
| 独立真实 Web consumer `make integration-consumer` | exit 0；19 passed、零 skip；其中5项文档真实消费者含JSON/空字节 | `integration/consumer-final.{json,log}`、`integration/consumer-final-report/` |
| Infinity 专项 `make integration-infinity` | exit 0；22 passed、零 skip；SDK/server 均 0.7.0.dev6 | `integration/infinity-final.{json,log}` |
| Go build/vet/3 CLI/server build | 全部 exit 0；Go 1.25.14、真实 native library/dictionaries | `integration/go-*-build-r2.json`、`integration/go-build-r2.json`、`integration/go-vet-r2.json` |
| 全量 `go test -race -count=1 -json ./internal/...` | exit 0；977 叶测试 passed，1095 全层 pass events；16 显式 live opt-in skip，7 无测试包；go-audit exit 0 | `integration/go-full-r2/{command.json,tests.jsonl,stderr.log,summary.json}`、`integration/go-full-audit-r2.json` |
| Web `npm run test:ci` | exit 0；1477 passed、零 skip（782 source、604 Vitest、81 desktop、10 tooling） | `integration/web-test-ci-r4.{json,log}` |
| Web lint/build/agent strict/typed lint/file size/bundle size | 全部 exit 0；lint 0 errors，1366 既有 warnings | `integration/web-*-r2.{json,log}` |
| 双 Python API 实际资源 smoke、跨副本 cookie、Go/Python auth 与实际 CLI | exit 0；双副本资源 health/smoke、跨副本cookie、身份互通及真实CLI五项检查通过 | `integration/smoke-hybrid-cli-r2.{json,log}`、`integration/smoke-hybrid-cli-r2-evidence/` |

Python/CLI五项为REGISTER、带凭据PING、LOGIN SQL、重复REGISTER拒绝、LOGOUT；
注册后独立SQL读回regular用户、bcrypt、tenant/membership及root folder。

最终 verify/core/consumer 的冻结后端输入为 `c4b2f8cc60dbcc0c1eaf025ec5ec6b93e0690b72`。
完整 Go build/vet/三命令构建/race 在 CLI 修复提交 `d08c8cae12be099348162ee6bda7d52e0904f104`
执行；到最终 c4 只改 integration 的 file-link 旁路任务回归，Go 全部编译/测试输入相同。
实际跨语言 smoke 使用 `d08c8cae` 编译 CLI 连接 `dd4e1e01` 的真实 Python API；
此后仅改 Go CLI 与 integration test/doc，已加载 API/server 生产输入不变。
二进制 SHA与两侧源码 SHA分别记录，不把这些运行混记为最终新 SHA 上的重跑。
Infinity 实际运行在后端 `73c9b4ee1adcd61e78c26427a7d787bdbb3ae54e`；
到最终输入只改变 consumer pin、integration 下载/file-link验收及 Go CLI/doc，
Infinity 源码、SDK/server和相关配置不变。新增 integration 测试已做定向 Ruff/真实验收。
Web 完整门禁运行在 `6d9e524...`，到最终 Web 只修改
实际下载验收脚本；该脚本用实际严格 tsconfig 加 Node 类型独立通过类型检查，并被
真实 HTTP 用例执行。逐路径复用依据见 `integration/final-evidence-reuse.json`，
不把旧 SHA 的执行记成新 SHA 上重跑。

本轮保留失败及其修复：

- 初次 verify 的共享 venv 使用 Infinity SDK dev5，而 lock 要求 dev6；工作树 launcher
  缺失和 umask 077 也影响环境/权限反例。原结果 6597 passed、3 failed，exit 2。
  新建本批专用 Python 3.12.9 frozen venv、恢复正常 022 umask，不修改共享 venv；
  三个原失败定向通过后完整 verify 6600 passed。首次自动选 Python 3.13 的 NumPy
  构建失败也保留日志，随后显式选择 3.12，不改 lock。
- 初次 core 因上述运行时差异主动中止；第二次 core 的私有 MinIO 覆盖漏掉 region，
  引发三个 beta download setup errors（286 passed、15 deselected、3 errors，exit 2）。
  修正私有覆盖的 region/bucket/prefix 默认值，保留原断言和生产实现；原 beta3 + 文档12
  共15项定向通过，之后以冻结输入重新完整执行 core。
- Web 全套分别发现 reducer 空格语义与旧 Studio 默认展开断言，修复后复跑通过；
  下载 JSON 丢字节先保存红测，再修下载 Response 处理及真实空/JSON 消费用例。
  全部失败日志在证据目录保留，未用跳过、放宽断言或旧批数字替换。
- 实际 API/CLI 首次 smoke 的 logout 401保留在 `integration/smoke-hybrid-cli.log`；
  双副本 health/cookie和身份互通当时已通过，不将整体失败计为通过。限定用户 logout
  的 Bearer 修复后，完整 Go及实际 API/CLI复验通过，旧失败未覆盖。
- 第三轮完整 core（`dd4e1e01`）1126 passed、1 failed、19 deselected，pytest exit1 / make exit2。
  唯一失败来自新增 file-link 测试对共享 scratch Task 整表为空的错误假设：11次拒绝和3次
  真正转换已执行完，本测试 Task已删，但先前用例的旁路Task仍在。独立SQL读回与显式种入
  sibling Task 的干净库红测共同复现。`c4b2f8cc...` 保留全表/xmin快照，要求仅删除
  被移除文档的Task，其他Task完整行必须保全；cleanup恢复原baseline，并新增旁路正控制。
  修后整文件2passed、14场景和独立SQL/MinIO/Redis absence通过；随后再跑最终完整core。
  该轮1405常规fixture文件共518286771字节已外置逐字节核对，见
  `integration/core-r3-fixture-export.json`、`integration/core-r3-fixture-evidence/`，
  JUnit/完整报告见`integration/core-r3-report/`。原失败没有计成完整通过。
- 隔离修复工作树首次定向收集缺少已忽略OCR模型文件；补齐仅指向共享资产的只读链接，
  保留原收集失败记录，再执行业务红/绿测试；共享资产未删除或替换。
- 独立脚本类型检查首次配置位于证据目录，未找到 Node 类型；配置显式引用本仓
  `node_modules/@types` 后严格检查通过，未改变仓库 TypeScript 策略。

最终 nodeid 分组核对见 `integration/final-suite-audit.json`：core 1127 与 consumer19
互不重叠，core排除的19项全由consumer实际运行；consumer因`-m external_consumer`
排除63项普通用例，均在完整core中覆盖。旧批1124节点全部仍在当前组合中，新增22节点，
当前标准组合1146节点；这里比较的是覆盖集合，不把旧批通过数当本批门禁。
Infinity22为单独后端专项。core/consumer/Infinity的JUnit失败、错误、skip均为0。

最终core常规fixture1405份，共518245515字节，consumer常规fixture19份，共13166字节，
已外置并逐文件SHA确认与原字节相等；pytest内部便利软链接重定向到导出目录内，
没有跟随外部链接。见`integration/{core,consumer}-fixture-export.json`及对应evidence目录。
全部原始`.test-results`另整体外置并核对，见`integration/all-backend-test-results-export.json`。
最终full-suite file-link案例又以新SQL/MinIO/Redis连接读回：五类归属行全0、桶/key absent，
原有1条Task完整行/xmin保全；14业务场景及旁路正控制通过，见
`integration/file-link-final-full-core-fresh-readback.json`。


## 清理与边界

原 12 个候选分支均在逐项接受及 `git cherry` 等价核验后删除，证据与 bundle
在 `integration/branch-cleanup-*.json`；各 owner 自有工作树已归档/移除。
SDK 及其他任务工作树/分支保留。镜像自动匿名卷在挂载核对时补登记，
以精确卷名和仅本批容器的挂载关系确认归属；见 `integration/owned-anonymous-volume.json`。本批集成工作树、专用 7 容器/4 命名卷及1镜像匿名卷/1 网络和私有
配置的最终清理读回见 `integration/cleanup-final.json`、`integration/cli-repair-worktree-cleanup.json`、
`integration/owned-fixture-temp-cleanup.json` 和 `integration/worktree-cleanup-final.json`；原 native/dictionary 资产只移除本批软链接，
不删除共享目标。

本批运行资源已按精确ID清理并在无标签过滤的完整清单中核实absent：7容器、
4命名卷+1匿名卷、1网络；5份运行私有配置和34条本批软链接已移除，共享目标仍在。
16个既存外部容器与初始相同ID/运行状态/启动时刻；独立CI的临时MinIO在本批清理前
已不存在，见`integration/docker-external-precleanup.json`。三个已登记且外置完成的
pytest目录已核实absent；临时CLI修复工作树已移除。本批集成工作树在文档提交后归档，
其分支按main包含关系清理；最终结果见`integration/worktree-cleanup-final.json`。

导出辅助脚本一度把清单写成最后一个fixture文件的名字；三份原清单和原材料均完整。
已修正变量名、恢复规范清单名，并再次核对全部导出文件原哈希，见
`integration/fixture-manifest-name-repair.json`；原执行脚本也另行保存。

实际 main 的无关 WIP 初始 40 路径；独立 CI 任务自行提交其中 12 路径，剩余
28 路径在本批接受窗口按内容 SHA 保持（共享 PROGRESS 的本批索引为单独提交增量）。
索引不包含其他任务改动，未 reset/stash/clean 无关内容。最终读回见
`integration/wip-final.json`，本批源码执行清单与文档提交的区别见
`integration/final-source-readback.json` 和 `integration/docs-only-final.json`。

默认 Go 的16个 live opt-in skip按允许清单显式审计；相关 provider/SQL及 hybrid auth
另行激活，不能把其余默认跳过的供应商/运行端点视为本次通过。完整默认 Go 的通过数
只统计实际完成测试，包/父测试事件与叶用例分开。未执行生产 Linux 二进制或部署验收。

真实外部边界：provider 测试使用受控 HTTP/实际 SDK，A/C 的相关 live opt-in
以隔离 PostgreSQL、已注册 Go 路由和实际 CLI 激活；本轮又执行实际 Python API
跨副本资源 smoke 与 CLI。未使用真实云模型/账户密钥，不宣称 Azure RBAC/HNS/SAS、
云供应商余额或嵌入质量、生产数据迁移或生产升级验证。

本轮另对consumer保存的20张连接器截图（1440x1000视口）拼成明/暗两张contact sheet并审阅，
未见阻塞布局问题；长SQL表单的完整截图高度1196，拼贴保留比例。
见`integration/consumer-visuals/{contact-sheet-manifest.json,assistant-visual-review.json}`。
此为单桌面视口的agent审阅，不代称人工或完整WCAG/移动端验收。
思考UI的contact sheet另含真实组件与受控数据的9视图（明暗/ZH/EN/键盘/RTL/代码/KaTeX/引用），不是完整产品页面后端 E2E。
转换授权测试覆盖预检拒绝前零副作用，未扩大为权限并发撤销或整批事务回滚保证。
未 push、未部署、未运行远程 CI；最终 merge queue/ci-required 仍须在推送后的最终
组合 SHA 上执行，不能由本机门禁替代。消费者 pin 为本地 Web 提交，远程运行前需
该提交在对应仓库可获取。
