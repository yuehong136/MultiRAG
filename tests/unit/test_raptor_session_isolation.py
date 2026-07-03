"""守护测试：LLM 长 IO 期间不得持有数据库会话（防 idle-in-transaction）。

历史问题：raptor/分析路径把调用方会话塞进 LLMBundle，外部模型调用期间
连接一直挂在事务里。现行防护是双层的：
1. 后台分析路径（raptor、文档分析、图片解析）构造 **分离的** bundle
   （db=None，用时自开短会话）；
2. LLMBundle 自身在 encode/chat 等长 IO 前调用 `_release_db_before_long_io`
   释放持有的会话（因此 pipeline_analysis 等传 self.db 的场景也安全）。

本文件用源码断言锁住这两层防护，避免无声回退。
"""

import inspect

from api.db.services import llm_service, tenant_llm_service
from api.db.services.document_analysis_service import DocumentAnalysisService
from api.db.services.pipeline_analysis_service import PipelineAnalysisService
from core.svr import task_executor
from deepdoc.parser import figure_parser


def test_llm_bundle_releases_db_before_long_io():
    # 方法定义在基类（tenant_llm_service），调用散布在 LLMBundle 各长 IO 入口
    assert "def _release_db_before_long_io" in inspect.getsource(tenant_llm_service)
    assert inspect.getsource(llm_service).count("self._release_db_before_long_io()") >= 2


def test_task_executor_raptor_uses_detached_bundle():
    source = inspect.getsource(task_executor)

    assert "raptor_chat_mdl = LLMBundle(None, chat_mdl.tenant_id" in source
    assert ".db = None" not in source


def test_document_analysis_service_uses_detached_bundles():
    source = inspect.getsource(DocumentAnalysisService)

    assert "LLMBundle(None, self.tenant_id" in source
    assert ".db = None" not in source


def test_pipeline_analysis_service_does_not_mutate_bundle_db():
    # pipeline_analysis 传 self.db（依赖 LLMBundle 的 release 防护），
    # 但不允许出现事后偷改 bundle.db 的 hack
    source = inspect.getsource(PipelineAnalysisService)

    assert ".db = None" not in source


def test_figure_parser_uses_detached_bundles():
    source = inspect.getsource(figure_parser)

    assert 'LLMBundle(None, kwargs["tenant_id"]' in source
    assert ".db = None" not in source
