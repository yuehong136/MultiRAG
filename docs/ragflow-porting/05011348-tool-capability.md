# 模型原生工具能力的保存与执行

本项对应 `05011348205f16973e8baa00e579bee9b7eab0a6`，冻结上限为
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。两个目标提交均已核实为该冻结的祖先；
上游 remote 为 `infiniflow/ragflow`，未 fetch。Python 基线为本批第 4 项
`f7aef1abd0c96d9c4ff2781f8d243528ec38ec54`，Web 基线为 `3358b65`。
Web 对应提交为 `84b36adfe8c84ae8c8d657f0e6e31db947f3dbb3`。

## 功能足迹与本地入口

| 目标足迹 | 本地实现与验证范围 |
| --- | --- |
| Agent 工具流失败输出 | `agent/component/agent_with_tools.py` 一次读取默认值；无默认值时保留 `_ERROR`，同时写 content 并产生错误片段。 |
| 模型新增、验证与列表 | `api/apps/llm_app.py` 严格布尔字段、缺省判断、凭据恢复、验证零写入，普通/详细列表均返回有效 is_tools。 |
| ID 与名称配置读取 | `api/db/joint_services/tenant_model_service.py` 复用统一解析，不再覆盖租户显式 false。 |
| 旧配置读取与实例构造 | `TenantLLMService` 统一 codec/目录回退；构造器传 provider 完整配置，bundle 读取解码后的能力。 |
| Web 请求类型 | `AddLlmParams` 与 `MyLLMModel` 表达能力；详细查询使用独立 cache key，保存失效仍覆盖它。 |
| 英文、中文 locale | `settings.models.localModel` 对齐新增字段、编辑标题、提示及描述。 |
| Web 编辑初始化 | 模型页读取详细列表，把真实选中行交给编辑器；接通既有模型卡片的编辑入口。 |
| Ollama 表单 | 从大弹窗抽取 local-model-fields，工具开关只面向 chat/image2text；新模型默认 chat，编辑未触碰能力时省略字段，显式 false 保留。 |

上表覆盖目标完整 9 文件的行为。后修链核对了 `8cb2bf04f` 的新 key 优先、
`ce3402cbb` 的缺省 key 恢复，以及 `dc4b82523` / `9d5950963` 的 provider/model
配置归属演进。本仓仍使用活动的 TenantLLM Python 链，复用现有 api_key 配置对象保存
is_tools，不增加数据库列或 HTTP 兼容信封；不搬入另一套 provider-instance 架构。
`4c97c9ad6` 的上游旧函数删除与 `670e68872` 的 Python 后端删除不用于撤销本地活动路径。

## 行为与改进依据

- 显式 true/false 优先于模型目录；缺省继承已有 override 或目录值。拒绝字符串/数字/null
  冒充布尔，避免 `bool("false")` 错误开启工具。
- 缺省或 null key 恢复当前租户、厂商和模型的凭据；非空新 key 替换旧 key。
  结构化配置按字段合并，保留 GroupId、provider_order、未知字段和现有能力。
  编辑能力时省略的 api_base/max_tokens 保留，provider 批量换 key 也不丢 GroupId/能力。
- codec 保留 `api_key_payload`，送给 provider 的配置去掉能力元数据，不嵌套 JSON key。
  三条配置读取链共用同一解析机制，减少后续修复分叉。列表不返回凭据。
- `LLMBundle.bind_tools` 已尊重能力开关，保留既有机制；Ollama 默认 chat 也已等价，
  未制造重复默认值策略。模型编辑名称只剥离对应厂商已存在的完整存储后缀。
- 本地视觉实现原无工具绑定接口，不能只显示 image2text 开关。现在在绑定工具时才懒建
  同厂商聊天后端，复用其工具循环；图片使用视觉层已有归一化，视频复用既有抽帧函数。
  普通视觉/视频调用保持原路径，调用方历史不原地修改。没有聊天工具后端的视觉厂商
  拒绝启用，避免保存后才触发 AttributeError。
- 探测流显式关闭，避免读取首片段后留下本次 SDK client。权限、MCP 授权与既有错误状态
  保留；配置了默认输出才采用该输出，无默认输出不伪造成功。

## 本次证据

- 隔离 `make verify` exit=0：格式、Ruff、8 条依赖契约、async DB 门禁、mypy 144 个
  源文件通过；unit **6319 passed**。新增视觉桥相关定向组 **41 passed**；此前模型保存、
  codec、配置读取、bundle 启停与诊断回归 **60 passed**。
- `REQUIRE_SERVICES=1 INTEGRATION_NO_TESTCONTAINERS=1 make integration
  INTEGRATION_SUITE=db INTEGRATION_WORKERS=1 TESTS=tests/integration/test_llm_tool_capability.py`
  exit=0，**3 passed，零 skip**。只有自有 PostgreSQL scratch 资源，没有启动共享全量集成。
- 真实 FastAPI 路由、SQL commit 与新 session 读回，覆盖缺省/false、三种读取入口、
  key 轮换、MiniMax GroupId、未知字段、verify 零写入、非法布尔零写入、租户隔离。
  真实 SDK 序列化后的 MiniMax/Ollama 请求覆盖开关启停、工具执行与后续历史；
  Ollama 视觉开/关工具时均断言图片字节保留。
- Web `npm run lint`、文件体积门禁、`npm run test:ci`、`npm run build` 均 exit=0；
  全量测试 **1462 passed**（771 source-node、600 source-vitest、81 desktop-node、10 tooling-node）。
  首轮无关 MCP 时序用例失败，单文件复跑及后续完整门禁均通过，未修改该用例。
- 实际生产表单组件的受控浏览器预览覆盖中英、明暗、1440×1000/1024×768，键盘切换、
  false 请求载荷与重新打开；先审阅带文件名、路由、视口的 contact sheet。页面到编辑弹窗、
  保存 payload 的链路另有真实 React DOM 回归。未发现字段裁切或横向溢出。

本地证据在隔离工作树 `.test-results/port-05011348/`，包含源码 SHA-256、SDK 请求断言、
SQL 读回/清理与最终日志；Web 的 `output/playwright/` 保存截图、contact sheet、检查日志
与源码 hash。自有租户/模型行清理后独立读回为零，scratch 数据库再用管理连接确认不存在；
自有 Vite 监听与浏览器已关闭。共享 checkout、PROGRESS.md、旧批验证源码、共享依赖和
常驻服务未被本项替换，未 push。

## 限制

SDK HTTP transport 受控，未调用真实 MiniMax/Ollama 服务或验证生产凭据、账单、模型质量。
路由集成测试使用受控身份及 signer，不代表真实登录/MCP 授权验收；视觉图片走真实请求
序列化，视频桥只验证抽帧结果衔接，未重新执行真实视频抽帧或远程视频推理。
浏览器预览的保存回调受控，与真实 SQL 保存验收分开，不宣称完整生产浏览器端到端通过。
LiteLLM logging worker 在独立集成进程退出时仍有后台协程告警；未改第三方工作队列，
不据此宣称其日志退出已验收。新 SDK client 的成功/异常/取消/提前关闭另有直接关闭断言。
