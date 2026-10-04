# Pipeline 文档清理开关

Parser 节点的 `setups.pdf/doc/docx/html.remove_header_footer` 默认是布尔值
`false`。Web 表单、DSL 保存和重载均保留该字段。旧 DSL 中的字符串
`"true"`、`"false"` 会归一为布尔值；`remove_toc` 使用相同合同，HTML 不再以
字符串作为默认值。

| 格式 | 开启后的行为 | 保留的行为与边界 |
| --- | --- | --- |
| PDF | 移除解析器标记的 header/footer/number、page header/footer/number 区域 | 不匹配 table header，不按正文内容猜测。保留 layoutno、author/abstract、媒体类型、页码及 bbox；纯文本解析无法识别页眉页脚。关闭仅表示不执行这一步额外过滤，底层解析器既有清理行为不变 |
| DOC | 请求 Tika XHTML，移除 header/footer 容器后生成文本段落 | 正文同名文字保留；需要 Tika 输出结构标签，JSON/Markdown 均适用 |
| DOCX | 在解析前清空标准、首页、奇偶页的页眉页脚部件 | 正文、表格、图片和 outline 保留，不用文本相等判断删除。现有 JSON 与 Markdown 引擎本来就只读取正文，因此关闭时也不会额外插入页眉页脚 |
| HTML | 移除 header/footer 元素及 banner/contentinfo role 容器 | 正文相同文字和表格保留；继续使用现有 HTML5 解析器处理无 body 的片段 |

PDF 的 `position_tag` 是 1-based 页码，但 `extract_positions` 返回 0-based 页码。
Pipeline 在只有 tag 时加回 1；显式 `positions` 和 `_pdf_positions` 优先，不能再
重复偏移。过滤只删除选中的块，不重新编号正文的页码或坐标。

回归覆盖位于
[`test_flow_parser_header_footer.py`](../tests/unit/test_flow_parser_header_footer.py)、
[`test_pdf_chunk_metadata.py`](../tests/unit/test_pdf_chunk_metadata.py) 和
[`test_pdf_bbox_batches.py`](../tests/unit/test_pdf_bbox_batches.py)。
[`test_pipeline_parser_cleanup.py`](../tests/integration/test_pipeline_parser_cleanup.py)
在 PostgreSQL scratch 库验证关闭、开启、再关闭的保存与独立 SQL 读回。
DOC 的单元回归替换 Tika 服务边界；真实 DOC 服务、OCR/provider 和持久化 API 的验收
需使用独立运行环境，不能用这些单元测试替代。
