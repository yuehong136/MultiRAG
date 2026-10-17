# MiniMax GroupId 请求合同

本项对应 `86fe78c73f523e95d129a1f7f1668366b6a14817`。上游为本机
`/Users/xldu/project/ragflow`，remote 为 `infiniflow/ragflow`，范围冻结于
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch。

完整 diff 仅修改 Python LiteLLM 的初始化与请求参数构造：从原始 key 或 JSON 配置
读取 `api_key/group_id`，把 GroupId 拼接到 api_base query。标题中的 header 不代表
实际实现。冻结内后续 `742188c3b` 是观测数据改动，未替换认证机制；
`670e68872` 删除上游 Python 后端，本仓活动 Python 路径不随之删除。

## 本地适配及改进依据

- 正式注册的 MiniMax 使用 `chat.py::LiteLLMBase`；包内 `MiniMaxChat` 保留同步调用。
  二者共用带类型的 key 解码与 URL/query 分离，接受原始字符串、JSON 对象字符串和
  配置字典。字典不原地修改；错误字段只返回固定诊断，不回显载荷。
- 认证为 `Authorization: Bearer <api_key>`，GroupId 进入最终请求 URL 的 query。
  配置的非空 group_id 覆盖已有 GroupId；没有配置值时保留 URL 中最后一个 GroupId。
  只发送一个 GroupId，正确编码特殊字符；其他重复 query 参数和空值保留。
- 不照搬字符串拼接：本机 OpenAI 2.30.0 会在 base_url 的 raw_path 后追加端点，
  带 query 的 base_url 不能作为可靠请求地址。LiteLLM 1.82.6 又会丢弃 extra_query，
  或在允许透传后把它放入 JSON body。SDK default_query 会改写重复参数名称并丢空值。
  因此使用 MiniMax 专属请求 hook，在 SDK 构造端点后写入 query；其他 provider
  保持原调用。新增 SDK client 的生命周期覆盖响应和流读取，退出即关闭。
- 原有 extra_headers 保留；普通流式入口也传递调用方的 extra_headers/extra_query。
  日志统一脱敏器补充 GroupId/group_id 的 URL 与 JSON 形态，隐藏完整分组标识。
  Go 实现与其已修复测试未修改。

## 本次验证与限制

`test_minimax_group_id.py` 的 20 个回归通过。同步 requests 和异步真实 LiteLLM/OpenAI
SDK 分别覆盖普通/流式、三种 key 形态；捕获已准备请求，检查 URL 路径、解码后的
query、多值/空值、Bearer、额外 header，以及 JSON body 没有认证 query 字段。
另验证成功、传输错误、取消和消费者提前关闭流时 client 已关闭。
连同日志、URL 归一化、工具历史相关回归，初轮 170 passed。

本次隔离 `make verify` exit=0：格式、Ruff、8 条依赖契约、async DB 门禁与 mypy
144 个源文件通过，unit **6279 passed**。最终认证专项为 **20 passed**。
日志和源文件 SHA-256 在本机 `.test-results/port-86fe78c7/`；原始运行日志亦保留于
`/tmp/multirag-86fe78c7-*.log`。
源码位于自有隔离工作树，基线 `9582bdb80f1a1be4505f4061d1abe71aa4cac63a`；
共享 checkout、PROGRESS.md、常驻服务及共享依赖均未修改。

请求传输由受控 HTTP transport 替换；未调用真实 MiniMax、验证生产凭据、账单归属、
限流或远程服务可用性。测试使用的 SDK logging worker 有未 await 协程告警，
不据此宣称第三方日志工作队列已验收；本项新增的 HTTP client 关闭另有直接断言。
