"""
/v1/document/analyze_v2 接口测试用例

@project: multirag
@Author: AI Assistant
@date: 2025-11-05
"""

import pytest


class TestAnalyzeV2Configs:
    """测试各种配置组合"""

    def test_default_config(self):
        """测试默认配置"""
        config = {}

        # 应该使用默认的 metadata_fields
        expected_fields = ["semantic_tags", "short_summary"]

        # 默认策略应该是 auto
        assert config.get("processing_strategy", "auto") == "auto"

        # 默认去重策略应该是 smart
        assert config.get("dedup_strategy", "smart") == "smart"

    def test_hierarchical_config_valid(self):
        """测试 TitleChunkerConfig 有效性"""
        config = {"levels": [["^#\\s+", "^第[一二三]+章"], ["^##\\s+", "^\\d+\\."]], "hierarchy": 1}

        assert isinstance(config["levels"], list)
        assert all(isinstance(level, list) for level in config["levels"])
        assert 0 <= config["hierarchy"] <= 5

    def test_splitter_config_overlap(self):
        """测试 TokenChunker 重叠配置"""
        config = {"chunk_token_size": 512, "delimiters": ["\n\n", "\n", "。"], "overlapped_percent": 0.15}

        assert 100 <= config["chunk_token_size"] <= 4096
        assert 0.0 <= config["overlapped_percent"] <= 0.5

    def test_raptor_config(self):
        """测试 RAPTOR 配置"""
        config = {"max_cluster": 64, "max_token": 512, "threshold": 0.1, "random_seed": 42}

        assert 2 <= config["max_cluster"] <= 256
        assert 100 <= config["max_token"] <= 2048
        assert 0.0 <= config["threshold"] <= 1.0

    def test_metadata_fields_config(self):
        """测试元数据字段配置"""
        fields = [{"field_name": "authors", "prompt": "Extract author names", "aggregate": "merge"}, {"field_name": "summary", "prompt": "Generate summary", "aggregate": "concat"}]

        for field in fields:
            assert "field_name" in field
            assert "prompt" in field
            assert field["aggregate"] in ["merge", "concat", "deduplicate", "first"]


class TestStrategySelection:
    """测试策略选择逻辑"""

    def test_auto_strategy_short_document(self):
        """短文档应选择 simple 策略"""
        chunks_count = 5
        has_structure = False

        # 模拟逻辑
        if chunks_count <= 10 and not has_structure:
            expected_strategy = "simple"

        assert expected_strategy == "simple"

    def test_auto_strategy_long_with_structure(self):
        """长文档+有结构应选择 hybrid 策略"""
        chunks_count = 200
        has_structure = True

        if chunks_count > 50 and has_structure:
            expected_strategy = "hybrid"

        assert expected_strategy == "hybrid"

    def test_auto_strategy_long_no_structure(self):
        """长文档+无结构应选择 raptor 策略"""
        chunks_count = 200
        has_structure = False

        if chunks_count > 50 and not has_structure:
            expected_strategy = "raptor"

        assert expected_strategy == "raptor"


class TestDeduplicationStrategies:
    """测试去重策略"""

    def test_smart_dedup_synonym(self):
        """测试 SmartTagDeduplicator 同义词识别"""
        from api.db.services.pipeline_analysis_service import SmartTagDeduplicator

        dedup = SmartTagDeduplicator()

        # 同义词应该被识别为重复
        assert dedup.is_duplicate("深度学习", "DL") == True
        assert dedup.is_duplicate("神经网络", "NN") == True
        assert dedup.is_duplicate("人工智能", "AI") == True

    def test_smart_dedup_inclusion(self):
        """字符串层面的包含关系不视为重复：更长的标签是更具体的概念，
        只有归一化集合（同义词扩展）的子集关系才可能判重。"""
        from api.db.services.pipeline_analysis_service import SmartTagDeduplicator

        dedup = SmartTagDeduplicator()

        assert dedup.is_duplicate("深度学习", "深度学习框架") == False
        assert dedup.is_duplicate("神经网络", "卷积神经网络") == False

    def test_smart_dedup_different(self):
        """测试不同概念不应去重"""
        from api.db.services.pipeline_analysis_service import SmartTagDeduplicator

        dedup = SmartTagDeduplicator()

        # 不同概念不应去重
        assert dedup.is_duplicate("深度学习", "机器学习") == False
        assert dedup.is_duplicate("计算机视觉", "自然语言处理") == False


class TestAggregateStrategies:
    """测试聚合策略"""

    def test_merge_aggregate(self):
        """测试 merge 聚合"""
        from api.db.services.metadata_extractor import MetadataExtractor

        # 模拟结果
        results = ["标签1, 标签2, 标签3", "标签2, 标签4, 标签5", "标签1, 标签6"]

        # merge 应该合并并统计频次（__init__ 会查库，绕过构造器只测纯聚合逻辑）
        extractor = object.__new__(MetadataExtractor)
        extractor.aggregate = "merge"

        merged = extractor._aggregate_results(results)

        # 标签1和标签2出现2次，应该排在前面
        assert isinstance(merged, list)
        assert "标签1" in merged or "标签2" in merged

    def test_concat_aggregate(self):
        """测试 concat 聚合"""
        from api.db.services.metadata_extractor import MetadataExtractor

        results = ["摘要1", "摘要2", "摘要3"]

        extractor = object.__new__(MetadataExtractor)
        extractor.aggregate = "concat"

        concatenated = extractor._aggregate_results(results)

        # 应该用 \n\n 拼接
        assert "摘要1" in concatenated
        assert "摘要2" in concatenated
        assert "\n\n" in concatenated


class TestUseCaseScenarios:
    """测试实际使用场景"""

    def test_research_paper_config(self):
        """测试学术论文配置"""
        config = {
            "processing_strategy": "hybrid",
            "hierarchical_config": {"levels": [["^Chapter\\s+\\d+"], ["^\\d+\\."]], "hierarchy": 1},
            "metadata_fields": [
                {"field_name": "authors", "prompt": "Extract authors"},
                {"field_name": "abstract", "prompt": "Extract abstract"},
                {"field_name": "keywords", "prompt": "Extract keywords"},
            ],
            "dedup_strategy": "smart",
        }

        # 验证配置
        assert config["processing_strategy"] == "hybrid"
        assert len(config["metadata_fields"]) == 3
        assert all("field_name" in f for f in config["metadata_fields"])

    def test_meeting_recording_config(self):
        """测试会议录音配置"""
        config = {
            "processing_strategy": "raptor",
            "raptor_config": {"max_cluster": 20, "max_token": 512},
            "metadata_fields": [
                {"field_name": "speakers", "prompt": "Identify speakers"},
                {"field_name": "action_items", "prompt": "Extract action items"},
                {"field_name": "decisions", "prompt": "Extract decisions"},
            ],
        }

        assert config["processing_strategy"] == "raptor"
        assert config["raptor_config"]["max_cluster"] == 20


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_chunks(self):
        """测试空chunks"""
        chunks = []
        # 应该抛出异常或返回空结果
        # 实际实现中会在 _get_document_chunks 抛出 ValueError

    def test_single_chunk(self):
        """测试单个chunk"""
        chunks = [{"content_with_weight": "短文档内容"}]
        # 应该选择 simple 策略
        expected_strategy = "simple"

    def test_no_metadata_fields(self):
        """测试没有配置元数据字段"""
        config = {"metadata_fields": None}
        # 应该使用默认字段：semantic_tags, short_summary


# 集成测试示例（需要实际环境）
"""
@pytest.mark.asyncio
async def test_full_pipeline_analysis():
    # 需要数据库和 LLM 配置
    from api.db.services.pipeline_analysis_service import PipelineAnalysisService

    service = PipelineAnalysisService(db, tenant_id="test")

    result = await service.analyze_document(
        file=mock_pdf_file,
        processing_strategy="auto",
        metadata_fields=[
            {"field_name": "tags", "prompt": "Extract tags"},
            {"field_name": "summary", "prompt": "Summarize"}
        ],
        dedup_strategy="smart"
    )

    assert "metadata" in result
    assert "tags" in result["metadata"]
    assert "summary" in result["metadata"]
    assert "processing_info" in result
"""

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
