# Local Go ingestion utilities

This package belongs to the parallel Go implementation. It does not run the
Python production ingestion worker or submit tasks to its queue.

`NewChunkEngine().Compile(dsl)` compiles a JSON pipeline with one required
`split` stage and optional `preprocess` and `postprocess` stages, in that order.
Unknown options, invalid expressions, duplicate stages, and unsupported
strategies fail compilation. `Explain` describes the compiled operators without
reading a document. `Execute` returns actual `ResultChunks` and propagates
condition failures. Sizes, merge targets, filters and character overlap count
Unicode code points. Compiled plans can be reused concurrently.

```json
{"pipeline":[{"operator":"split","strategy":"sentence","params":{"boundaries":["。","\n"],"keep_separators":true}},{"operator":"postprocess","merge":{"target_size":500,"strategy":"greedy"},"overlap":{"enabled":true,"unit":"char","default":{"size":30}}}]}
```

Supported split strategies are `sentence` (literal boundaries, first match
wins), `paragraph` (nonempty trimmed lines), and `char` (individual code points).
Preprocess supports `normalize_newlines`, `strip_whitespace`,
`remove_empty_lines`, and `soft_line_break_merging`. Postprocess supports greedy
merge, conditional overlap (`if` with boolean/comparison expressions), length
filtering and metadata. Overlap reads the previous original merged chunk so it
does not recursively accumulate earlier overlaps. `language` metadata is a
Chinese/English heuristic, not language identification by a model.

`parser.Parse(filename, data, config)` requires `lib_type` and returns `Format`,
`Text`, `Markdown`, and `Backend`; it never prints. `builtin` supports body
paragraph/table-cell text in DOCX, presentation-order slide text in PPTX, and
worksheet values in XLSX (shared/inline strings, coordinates and cached formula
values). It does not calculate formulas, recognize images, preserve Office
layout or reproduce rich text formatting. Blank content in a valid document can
produce empty text. Malformed archives and unavailable parts fail explicitly.
Input is capped at 32 MiB; builtin ZIP expansion at 64 MiB and 4096 entries.

`office_oxide` delegates Office extraction to the pinned native binding. Build
with `-tags office_oxide` and the header/library prefix selected in `build.sh`.
Missing native builds, unsupported formats, open failures, text failures and
Markdown failures are errors. DOC/PPT/XLS support depends on the native library
and the actual input, and must be validated with real files. This package does
not supply a PDF parser, OCR, image extraction, or a remote-provider fallback.
Only the selected backend runs; unknown configuration keys are errors.
