import asyncio

import core.flow.utils as flow_utils
from core.flow.utils import hierarchical_merge, split_chunks


def test_utils_token_chunker_reuses_runtime_json_fields_and_raw_image():
    chunks = asyncio.run(
        split_chunks(
            {
                "output_format": "json",
                "json": [
                    {
                        "text": "Intro text.",
                        "doc_type_kwd": "text",
                        "position_int": [[0, 10, 20, 30, 40]],
                    },
                    {
                        "text": "Picture caption.",
                        "doc_type_kwd": "image",
                        "image": "raw-image",
                    },
                ],
            },
            chunk_token_size=1024,
            delimiters=[],
            image_context_size=16,
        )
    )

    assert chunks[0]["doc_type_kwd"] == "text"
    assert chunks[0]["position_int"] == [(1, 10, 20, 30, 40)]
    assert chunks[0]["page_num_int"] == [1]
    assert chunks[1]["doc_type_kwd"] == "image"
    assert chunks[1]["image"] == "raw-image"
    assert "Intro text." in chunks[1]["text"]


def test_utils_title_chunker_uses_runtime_hierarchy_and_preserves_source_chunks():
    result = asyncio.run(
        hierarchical_merge(
            [
                {"content_with_weight": "1 Intro", "position_int": [(1, 1, 2, 3, 4)]},
                {"content_with_weight": "Intro body"},
                {"content_with_weight": "2 Next", "position_int": [(2, 1, 2, 3, 4)]},
                {"content_with_weight": "Next body"},
            ],
            levels=[[r"^\d+ "]],
            hierarchy=1,
        )
    )

    chapters = result["chapters"]
    assert [chapter["chunk_indices"] for chapter in chapters] == [[0, 1], [2, 3]]
    assert chapters[0]["title"] == "1 Intro"
    assert chapters[0]["position_int"] == [(1, 1, 2, 3, 4)]
    assert [chunk["content_with_weight"] for chunk in chapters[1]["chunks"]] == ["2 Next", "Next body"]


def test_utils_removed_old_splitter_aliases():
    assert hasattr(flow_utils, "FlowTokenChunker")
    assert hasattr(flow_utils, "FlowTitleChunker")
    assert not hasattr(flow_utils, "FlowSplitter")
    assert not hasattr(flow_utils, "FlowHierarchicalMerger")


def test_analyze_document_request_preserves_chunker_config_fields():
    from api.apps.document_app import AnalyzeDocumentRequest

    request = AnalyzeDocumentRequest.model_validate(
        {
            "processing_strategy": "hierarchical",
            "splitter_config": {
                "chunk_token_size": 512,
                "delimiters": ["\n\n", "\n"],
                "overlapped_percent": 0.1,
                "children_delimiters": ["@@"],
            },
            "hierarchical_config": {
                "levels": [[r"^\d+ "]],
                "hierarchy": 1,
                "method": "group",
                "include_heading_content": True,
            },
        }
    )

    assert request.splitter_config.model_dump() == {
        "chunk_token_size": 512,
        "delimiters": ["\n\n", "\n"],
        "overlapped_percent": 0.1,
        "children_delimiters": ["@@"],
    }
    assert request.hierarchical_config.model_dump() == {
        "levels": [[r"^\d+ "]],
        "hierarchy": 1,
        "method": "group",
        "include_heading_content": True,
    }
