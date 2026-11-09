# Local Go ingestion utilities

This package belongs to the parallel Go implementation. It does not run the
Python production ingestion worker or submit tasks to its queue.

`NewChunkEngine().Compile(dsl)` compiles the declared JSON pipeline in its given
order. Stages may repeat; no fixed order, stage count or required `split` stage
is imposed. A missing or empty pipeline compiles to an empty plan. Invalid
operator options, expressions and unsupported strategies fail compilation.
`ChunkPlan.Operators` exposes the ordered operators. `Explain` describes them without
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
Input is capped at 32 MiB; builtin ZIP expansion, cumulative part reads and
combined Text/Markdown output each at 64 MiB, with at most 4096 ZIP entries.
Repeated part and shared-string references remain valid within those budgets.

`office_oxide` delegates Office extraction to the pinned native binding. Build
with `-tags office_oxide` and the header/library prefix selected in `build.sh`.
Missing native builds, unsupported formats, open failures, text failures and
Markdown failures are errors. DOC/PPT/XLS support depends on the native library
and the actual input, and must be validated with real files. This package does
not supply a PDF parser, OCR, image extraction, or a remote-provider fallback.
Only the selected backend runs; unknown configuration keys are errors.

Native verification requires a registered fixture directory containing
`sample.doc`, `sample.docx`, `sample.xls`, `sample.xlsx`, `sample.ppt` and
`sample.pptx`, passed as `MULTIRAG_OFFICE_FIXTURES`. Run
`go test -tags office_oxide ./internal/ingestion/parser` with the selected
header/static-library flags. Those files are test inputs, not production data.
On macOS, use the release static archive: the tested v0.1.2 dynamic library
contains an absolute build-machine install name. The build entry point selects
the prepared static archive without rewriting the library or global caches.

Native extraction performs allocations inside the external library before Go can
check the returned Text/Markdown size. The result-size limit is not a hard
native peak-memory bound; compressed Office inputs also receive archive-size
preflight validation, but native internals are not sandboxed by this package.
