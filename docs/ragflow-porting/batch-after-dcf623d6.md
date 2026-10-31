# dcf623d6 后二十项本地交付

2026-10-31 冻结上游第一父序列 `(dcf623d60dde9620278a7827778eb2cea6f0c99a,32559d2dfca8909749f65475a0d7cd3bf8e0c332]`，连续20项、62条逐提交路径；第16项第一父差异为空，目录行为由第15项完整覆盖。参考上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，未fetch、未扩批。后端基线 `bf3414184b999683419dba324a450c53aafdc029`，Web基线 `6bea392bee56268ecaa47ee5b4fc946d46253c19`。

实际可执行组合为 `9d085867cd1cd00f67a334a16fa0f925ac9f6772`；其后仅提交本记录。Web/master与 `.github/consumer-web.json` 固定 `0833c49c643ac266aead69169f25c01af88496b3`。逐路径处置、接收映射、原始命令/输入/exit/JUnit、源码恢复与清理读回均在本地证据根 `/Users/xldu/.local/state/multirag/validation/20261031-batch-after-dcf623d6`，以 `delivery-complete.json`、`item-ledger.json`、`intake.json`、`validation.json`、`cleanup.json` 为准。

## 逐项处置

| 项 / 上游 SHA | 当前行为与质量选择 |
|---|---|
| 1 / `357cb84c` | Cohere聊天使用显式 `cohere_chat/` 路由；保留真实normal/stream/tools消费者、过滤、重试和流关闭边界。独立embedding/rerank不变。 |
| 2 / `139f4515` | 保留活跃typed CLI/client/registry/CE模式与重试。仅登录失败屏蔽服务端敏感正文，保留HTTP/业务码与errors.Is/As；移除重复错误打印。 |
| 3 / `3796835c` | Go Agent文件下载走JWT/租户/严格输入与二进制响应合同，绑定MinIO/S3/OSS的30秒context读取和body关闭；真实Router消费者及失败无泄露回归。 |
| 4 / `16d5b4fa` | Go同步文件转知识库，保留SQL计数、对象复制、Redis任务信封、失败补偿与源文件；PG固定连接咨询锁覆盖新增到旧资源退休，实际地址保护共享对象。仅支持同租户目标，见专门合同。 |
| 5 / `3091d91c` | 当前SQL已稳定按排序键与ID分页，无inactive分组；保留机制并用真实SQL证明跨页次序。 |
| 6 / `8e17a129` | 当前共享think解析器已逐片即时yield；补消费者在producer暂停时已收到首字的时序回归，不回退split marker和累计/delta处理。 |
| 7 / `ad63877f` | Web Bedrock已有typed auth/region，Verify和Save使用同一候选；保留并验证，不恢复旧表单字段。 |
| 8 / `7355db18` | OCR保留numeric/default/owner/active/env实例身份贯穿naive与pipeline；Web三个OCR表单保留ocr能力与max_tokens=0，修正失效测试假件。 |
| 9 / `0d3e4108` | LocalAI精确callable ID优先；仅在权威/v1/models确有候选时归一终端tag，不误删registry端口或任意名字片段。 |
| 10 / `2980981d` | LLM/工具Agent逐轮按声明能力选chat/vision，配置相同复用、变更重建；关闭旧客户端、取消drain、DB脱离及工具session/max_rounds保留。同Canvas关闭失败后下一轮能恢复。 |
| 11 / `aafe6c55` | 保留当前检索total、page/size及父子合并合同；真实REST/SQL/Milvus和Web提交快照、翻页、过滤/页大小重置已验证，不移入过时TODO或取消分页的后续合同。 |
| 12 / `1fd9e1df` | 为实际@js-preview/excel消费者增加限定作用域的细滚动条、8px轨道与overlay可见背景，真实双轴滚动、亮暗和窄屏通过。 |
| 13 / `acaeb416` | FishAudio已有非空音频字节验证、生成器关闭和取消drain；补真实adapter受控SDK→REST→独立SQL的成功/空输出/错误无写入验收。 |
| 14 / `dfa4c5a7` | 保留小写canonical能力、图标和翻译；补标签语义排序。 |
| 15 / `515acf4f` | Go目录530项，454项Qwen、70项DeepSeek与其他6项；既有76项不变。去932个大小写冗余alias后上游每个key仍解析，保留现有原子加载、大小写查找与深拷贝。 |
| 16 / `e132173d` | 第一父差异为空，不伪造独立改动；第15项目录与真实发现HTTP/CLI/SQL覆盖该行为。 |
| 17 / `3e4fb8cf` | 保留当前MCP transport/SSRF/header/session/timeout合同及活跃CLI测试；修复run_go_tests.sh为仓库根相对的完整internal race发现与真实exit传递。 |
| 18 / `dfcf226b` | Go按owner/tenant授权并保持锁到写入结束；ES与Infinity独立memory schema、物理ID/业务ID验证、首个遗忘时间、乐观写入和独立读回。缺能力后端失败关闭。 |
| 19 / `bf59eb77` | Go验证码/OTP/恢复密码，真实SQL/Redis与受控SMTP；proof绑定短期HttpOnly客户端cookie、原子消费及Origin限制。Web四调用携带凭证，图片/bool/无自动登录保持。未知提交不恢复proof；实际PG受控Commit故障回归见收据。 |
| 20 / `32559d2d` | 三个空OCR目录以provider级supported_capabilities发布ocr；实际Web供应商卡与筛选可用，不制造model/instance占位。 |

## 验证与版本绑定

Python是生产后端：组合定向unit **534通过、0skip**，SQL/SDK集成 **23通过、0skip**，SQL/Milvus/MinIO检索专项 **2通过、0skip**。服务先于收集准备，`REQUIRE_SERVICES=1`，scratch独立读回。改动Python的Ruff/format、import-linter、async DB及当前纳管mypy通过；最终CI字节另以38项CI单测、定向类型检查和YAML/bash/嵌入Python语法检查补验。供应商为受控边界，未验付费账号或常驻worker。

Go为停滞的并行实现：组合定向race **209个顶层用例、532条含子测试通过事件、0skip**；5个真实tokenizer并发/native用例均通过。复用已保留且可核来源的真实native archive，C++树、Go锁、平台、PCRE2/SIMDe和字典均与构建证明匹配；没有本批重新构建native的声明。Go运行绑定`1b857d4f`；随后`9d085867`仅补两项Python CI必跑名称。1110个Go/C++/锁/目录输入逐字节比对不变。Go1.25.14/Darwin arm64，完整internal build/vet、三个main独立链接和受影响race/native消费者保留。原始JSON、首错、exit和派生JUnit独立保存。

独立Web：W的63项Vitest、26项Node、6项最终Excel回归及build/lint/inventory/size/format按未变输入作用域复用。I补修0833运行13项认证/登录隔离Node、局部lint/format与生产build；真实Web authAPI/apiClient和两独立Chromium context验证跨端口cookie、错客户端拒绝、正确客户端成功、cookie删除和旧登录保留，HTTP服务为受控合同。P20实际Python REST→SQL→生产Web浏览器另有8次真实库存请求，亮暗/390px与键盘筛选contact sheet已审。后续0833仅改变四个恢复方法和其测试，供应商/Excel/检索消费者字节不变，未重复无关浏览器场景。未声称本地完整test:ci通过。

最终CI保留完整internal race、此前两组Go integration、Python完整分组及ci-required。新增Qwen、文件、ES/Infinity memory、恢复流程与提交故障必跑，skip/缺失/失败均拒绝；私有动态端口服务与label归属清理失败关闭。CI配置已提交，远程工作流仍待运行。

## 清理与边界

每个owner在接收后验证bundle实际恢复与blob，随后精确移除工作树/ref/实际admin；源码提交保留在main/master，原始首错和恢复bundle留证据根。主树原28项WIP逐次哈希不变；未改写其脏PROGRESS，批次事实仅在本记录维护。I独立读回确认110个scratch数据库、54个MinIO桶、15个Milvus collection、7个ES索引、600个Redis精确key与49个键模式、211个登记listener均不存在。独占Infinity容器及其数据已移除，借用镜像和现有业务服务保留。I最终临时源码、模块缓存、私有覆盖和二进制的退役读回由cleanup.json记录。

`strictProtocolPassed=false`：I/S首次worktree创建的扩展登记未先成功落盘；S工作树logs/Ruff叶路径登记迟于首次创建，且共享测试收集文件未遵守I独占写窗。影响仅自有隔离资源，收集分类已由I复核接受，但不能追认为严格事前登记。原始失败均保留，重验使用新run ID。

Go详细接口与限制见[文件、memory、恢复密码合同](go-file-memory-recovery.md)。文件转换以PostgreSQL和同租户目标为界，Python当前允许的授权跨租户绑定不宣称Go已完全等价；旧Put调用虽等待/补偿，不属于下载30秒context读取保证。Memory的Milvus能力仍不支持。提交故障使用真实PostgreSQL与受控事务返回值，不等于真实网络故障注入；TLS/STARTTLS认证与真实邮件供应商未实测。

Python生产恢复密码仍使用历史邮箱全局证明，本批Go客户端绑定修复不代表该Python风险已修复。扩展P单测保留一条基线VolcEngine registry旧期望失败；扩展非纳管mypy三条基线错误仍保留，不能称全库质量全绿。未push、未部署、未跑远程CI、未做完整生产/真实供应商验收，不推进下一批。
