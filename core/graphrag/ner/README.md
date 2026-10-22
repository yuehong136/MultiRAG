# Local NER extraction

Set `parser_config.graphrag.method` to `ner` to extract entities with spaCy NER
and three-stage keyword stacking. Relations describe sentence co-occurrence;
they are contextual links, not verified factual claims. Light remains the default,
and General keeps its LLM prompt extraction.

The worker selects `en_core_web_sm` or `zh_core_web_sm` from the dataset's
`language` (passed as `task.language`). English/en and Chinese/zh locale aliases
are accepted; other languages fail explicitly. This contract has no separate
`parser_lang` setting. Product UI language does not select the NLP model.

`uv sync --group dev --frozen` installs spaCy and both pinned model wheels from
`pyproject.toml`/`uv.lock`. Inference never downloads models. Missing/corrupt models
and chunk inference errors fail the document without publishing a partial graph.
Models are cached per process and inference runs off the event loop under a lock;
cancellation drains inference before releasing the graph writer lease.

NER preserves source chunk boundaries and sentence text, including repeated entity
occurrences and source IDs. The default links only entities sharing a sentence.
Entity-type filters apply to both nodes and relation endpoints. Extraction itself
makes no LLM calls. Description summarization for many duplicates, optional entity
resolution and community reports may still use the configured tenant chat model;
embedding and existing checked graph persistence remain active.

Focused regressions: `tests/unit/test_graphrag_ner.py` uses both installed models;
`tests/integration/test_graphrag_ner.py` verifies real HTTP config, independent SQL,
Milvus graph/entity/relation checkpoints, graph reads and replay. Model quality on
arbitrary domain text, paid providers and production deployment are separate
acceptance layers.
