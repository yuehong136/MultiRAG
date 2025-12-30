# core/flow/utils - 组件纯函数提取工具

## 📖 目录说明

此目录包含 `core/flow/` 各组件的**纯函数提取版本**，去除 Canvas/DSL/Graph 框架依赖，可直接在 analyze_v2 等场景中使用。

## 📁 文件对应关系

```
core/flow/utils/
├── parser_utils.py              ← 对应 core/flow/parser/
├── splitter_utils.py            ← 对应 core/flow/splitter/
├── hierarchical_merger_utils.py ← 对应 core/flow/hierarchical_merger/
└── extractor_utils.py           ← 对应 core/flow/extractor/
```

**原则：一个 utils 文件对应一个组件目录**

## 🔧 维护指南

### 当 core/flow 组件更新时

1. **定位对应的 utils 文件**
   ```
   core/flow/parser/parser.py 更新
     ↓
   core/flow/utils/parser_utils.py 需要同步
   ```

2. **检查变更内容**
   - 核心算法是否改变？
   - 参数签名是否变化？
   - 返回格式是否调整？
   - 依赖库 API 是否有更新？（如 check_installation 返回值变化）

3. **同步更新 utils**
   - 更新核心逻辑
   - 更新参考注释（行号）
   - 更新错误处理逻辑
   - 测试验证

### 注释规范

每个函数都标注了参考来源：

```python
async def parse_audio(...):
    """
    音频解析（参考 core/flow/parser/parser.py._audio 第 541-558 行）
                ↑                              ↑           ↑
             组件文件                        方法名      行号范围
    """
```

**修改 core/flow 后记得更新行号！**

## 📚 使用示例

### 基础使用

```python
from core.flow.utils import parse_file, split_chunks

# 解析（使用默认 deepdoc）
parsed = await parse_file(filename, binary, tenant_id)

# 切分
chunks = await split_chunks(parsed, overlapped_percent=0.1)
```

### 使用 TCADP parser（腾讯云 ADP）

```python
from core.flow.utils import parse_file

# PDF 使用 TCADP 解析
parsed = await parse_file(
    filename="document.pdf",
    binary=file_content,
    tenant_id=tenant_id,
    pdf_config={
        "parse_method": "tcadp parser",
        "output_format": "json",
        "table_result_type": "1",
        "markdown_image_response_type": "1"
    }
)

# Excel 使用 TCADP 解析
parsed = await parse_file(
    filename="data.xlsx",
    binary=file_content,
    tenant_id=tenant_id,
    excel_config={
        "parse_method": "tcadp parser",
        "output_format": "html"
    }
)

# PPT 使用 TCADP 解析（支持 .ppt 和 .pptx）
parsed = await parse_file(
    filename="presentation.pptx",
    binary=file_content,
    tenant_id=tenant_id,
    slides_config={
        "parse_method": "tcadp parser",
        "output_format": "json"
    }
)
```

### 高级组合

```python
from core.flow.utils import (
    parse_file, 
    split_chunks, 
    hierarchical_merge
)

# Parser → Splitter → HierarchicalMerger
parsed = await parse_file(...)
chunks = await split_chunks(parsed, ...)
hierarchy = await hierarchical_merge(chunks, ...)
```

## ⚠️ 重要提示

### 不要直接修改此目录文件

**正确流程：**
1. 先修改 `core/flow/xxx/` 原组件
2. 再同步到 `core/flow/utils/xxx_utils.py`

**原因：**
- 原组件是真理来源（Canvas/Workflow 使用）
- utils 是提取版本（analyze_v2 使用）
- 保持一致性很重要

### 添加新功能

**示例：添加新的解析器类型**

1. 在 `core/flow/parser/parser.py` 添加 `_video()` 方法
2. 在 `parser_utils.py` 添加 `FlowParser.parse_video()` 方法
3. 在 `parse_file()` 中添加文件类型判断

## 🧪 测试

```bash
# 测试所有 utils
python -m pytest tests/test_flow_utils.py

# 测试特定组件
python -m pytest tests/test_flow_utils.py -k "parser"
python -m pytest tests/test_flow_utils.py -k "splitter"
```

## 📊 代码统计

| 文件 | 行数 | 类 | 函数 |
|------|------|---|------|
| parser_utils.py | 1021 | 1 | 10 |
| splitter_utils.py | 251 | 1 | 3 |
| hierarchical_merger_utils.py | 208 | 1 | 2 |
| extractor_utils.py | 122 | 1 | 2 |
| **总计** | **1602** | **4** | **17** |

## 🎯 设计原则

1. **简单优于复杂**：去除框架，保留核心
2. **明确对应关系**：一个组件 → 一个 utils
3. **便于追踪**：注释标注参考来源
4. **易于维护**：清晰的目录结构

