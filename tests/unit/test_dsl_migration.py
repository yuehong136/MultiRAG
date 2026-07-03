import json

from agent.dsl_migration import normalize_chunker_dsl


def test_normalize_chunker_dsl_rewrites_legacy_components_and_refs():
    legacy = {
        "path": ["Parser:One", "Splitter:Old", "HierarchicalMerger:Merge"],
        "components": {
            "Parser:One": {
                "downstream": ["Splitter:Old"],
                "obj": {"component_name": "Parser", "params": {}},
            },
            "Splitter:Old": {
                "upstream": ["Parser:One"],
                "downstream": ["HierarchicalMerger:Merge"],
                "obj": {
                    "component_name": "Splitter",
                    "params": {
                        "messages": [
                            {"content": "Use {Splitter:Old@chunks}"},
                            {"content": "{{ HierarchicalMerger:Merge@chunks }}"},
                        ],
                        "reference": "{Splitter:Old@chunks}",
                    },
                },
            },
            "HierarchicalMerger:Merge": {
                "parent_id": "Splitter:Old",
                "obj": {
                    "component_name": "HierarchicalMerger",
                    "params": {"prompts": "Summarize {Splitter:Old@chunks}"},
                },
            },
        },
        "graph": {
            "nodes": [
                {
                    "id": "Splitter:Old",
                    "type": "splitterNode",
                    "data": {
                        "label": "Splitter",
                        "name": "Splitter",
                        "form": {"prompts": "{Splitter:Old@chunks}"},
                    },
                },
                {
                    "id": "HierarchicalMerger:Merge",
                    "type": "chunkerNode",
                    "parentId": "Splitter:Old",
                    "data": {"label": "HierarchicalMerger", "name": "HierarchicalMerger"},
                },
            ],
            "edges": [
                {
                    "id": "xy-edge__Parser:Onestart-Splitter:Oldend",
                    "source": "Parser:One",
                    "target": "Splitter:Old",
                },
                {
                    "id": "xy-edge__Splitter:Oldstart-HierarchicalMerger:Mergeend",
                    "source": "Splitter:Old",
                    "target": "HierarchicalMerger:Merge",
                },
            ],
        },
        "history": [{"content": "{Splitter:Old@chunks}"}],
        "messages": [{"content": "{HierarchicalMerger:Merge@chunks}"}],
        "reference": {"from": "Splitter:Old"},
    }

    migrated = normalize_chunker_dsl(legacy)

    assert "Splitter:Old" in legacy["components"]
    assert "TokenChunker:Old" in migrated["components"]
    assert "TitleChunker:Merge" in migrated["components"]
    assert "Splitter:Old" not in migrated["components"]
    assert "HierarchicalMerger:Merge" not in migrated["components"]

    assert migrated["path"] == ["Parser:One", "TokenChunker:Old", "TitleChunker:Merge"]
    assert migrated["components"]["Parser:One"]["downstream"] == ["TokenChunker:Old"]
    assert migrated["components"]["TokenChunker:Old"]["downstream"] == ["TitleChunker:Merge"]
    assert migrated["components"]["TitleChunker:Merge"]["parent_id"] == "TokenChunker:Old"
    assert migrated["components"]["TokenChunker:Old"]["obj"]["component_name"] == "TokenChunker"
    assert migrated["components"]["TitleChunker:Merge"]["obj"]["component_name"] == "TitleChunker"

    serialized = json.dumps(migrated, ensure_ascii=False)
    assert "Splitter:Old" not in serialized
    assert "HierarchicalMerger:Merge" not in serialized
    assert "splitterNode" not in serialized
    assert "TokenChunker:Old@chunks" in serialized
    assert "TitleChunker:Merge@chunks" in serialized


def test_normalize_chunker_dsl_ignores_non_pipeline_payloads():
    assert normalize_chunker_dsl(None) is None
    assert normalize_chunker_dsl({"not_components": []}) == {"not_components": []}
