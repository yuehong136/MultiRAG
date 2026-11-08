package ingestion

import (
	"strings"
	"sync"
	"testing"
	"unicode/utf8"

	"multirag/internal/testsupport"
)

func TestChunkPipelineOutputsAndReuse(t *testing.T) {
	testsupport.NewCase(t)
	engine := NewChunkEngine()
	plan, err := engine.Compile(`{"name":"Chinese","pipeline":[{"operator":"preprocess","strip_whitespace":true},{"operator":"split","strategy":"sentence","params":{"boundaries":["。"],"keep_separators":true}},{"operator":"postprocess","merge":{"target_size":7,"strategy":"greedy"},"overlap":{"enabled":true,"unit":"char","default":{"size":1}},"add_metadata":{"include_index":true,"custom_fields":{"length":"auto_detect"}}}]}`)
	if err != nil {
		t.Fatal(err)
	}
	explanation, err := engine.Explain(plan)
	if err != nil || !strings.Contains(explanation, "target_size=7") || !strings.Contains(explanation, "enabled=true") {
		t.Fatal(explanation, err)
	}
	var group sync.WaitGroup
	for range 8 {
		group.Go(func() {
			out, err := engine.Execute(plan, "  你好。世界。最后。 ")
			if err != nil {
				t.Error(err)
				return
			}
			if len(out.ResultChunks) != 2 || out.ResultChunks[0].Content != "你好。 世界。" || out.ResultChunks[1].Content != "。最后。" {
				t.Errorf("lost chunk content: %#v", out.ResultChunks)
				return
			}
			for i, c := range out.ResultChunks {
				if c.Index != i || c.Size != utf8.RuneCountInString(c.Content) || c.Metadata["index"] != i || c.Metadata["length"] != c.Size {
					t.Errorf("bad chunk metadata: %#v", c)
				}
			}
		})
	}
	group.Wait()
}
func TestChunkSplitWithoutOptionalStages(t *testing.T) {
	testsupport.NewCase(t)
	e := NewChunkEngine()
	plan, err := e.Compile(`{"pipeline":[{"operator":"split","strategy":"paragraph"}]}`)
	if err != nil {
		t.Fatal(err)
	}
	c, err := e.Execute(plan, "one\n\n二")
	if err != nil || len(c.ResultChunks) != 2 || c.ResultChunks[1].Content != "二" || c.ResultChunks[1].Size != 1 {
		t.Fatal(c, err)
	}
}
func TestChunkRejectsInvalidAndIgnoredOptions(t *testing.T) {
	testsupport.NewCase(t)
	for _, dsl := range []string{
		`null`, `{}`, `{"pipeline":[]}`, `{"pipeline":[{"operator":"preprocess"}]}`,
		`{"pipeline":[{"operator":"split","strategy":"unknown"}]}`,
		`{"pipeline":[{"operator":"split","params":{"boundaries":[""]}}]}`,
		`{"pipeline":[{"operator":"split","params":{"boundaries":[12]}}]}`,
		`{"pipeline":[{"operator":"split","typo":true}]}`,
		`{"pipeline":[{"operator":"postprocess"},{"operator":"split"}]}`,
		`{"pipeline":[{"operator":"split"},{"operator":"split"}]}`,
		`{"pipeline":[{"operator":"split"},{"operator":"postprocess","overlap":{"conditions":[{"if":"length >","then":{"size":1}}]}}]}`,
		`{"pipeline":[{"operator":"split"},{"operator":"postprocess","overlap":{"default":{"size":-1}}}]}`,
		`{"pipeline":[{"operator":"split"},{"operator":"postprocess","filter":{"min_length":10,"max_length":1}}]}`,
	} {
		if _, err := NewChunkEngine().Compile(dsl); err == nil {
			t.Errorf("invalid DSL succeeded: %s", dsl)
		}
	}
	if _, err := NewChunkEngine().Execute(nil, "input"); err == nil {
		t.Error("nil plan succeeded")
	}
}
func TestOverlapConditionFailureAndDisabled(t *testing.T) {
	testsupport.NewCase(t)
	e := NewChunkEngine()
	for _, enabled := range []string{"true", "false"} {
		p, err := e.Compile(`{"pipeline":[{"operator":"split","strategy":"char"},{"operator":"postprocess","overlap":{"enabled":` + enabled + `,"conditions":[{"if":"unknown = true","then":{"size":1}}]}}]}`)
		if err != nil {
			t.Fatal(err)
		}
		c, err := e.Execute(p, "abc")
		if enabled == "true" && err == nil {
			t.Error("unknown variable swallowed")
		}
		if enabled == "false" && (err != nil || len(c.ResultChunks) != 3 || c.ResultChunks[1].Content != "b") {
			t.Fatal(c, err)
		}
	}
}

func TestExplainShowsOverlapUnitOverride(t *testing.T) {
	testsupport.NewCase(t)
	e := NewChunkEngine()
	p, err := e.Compile(`{"pipeline":[{"operator":"split"},{"operator":"postprocess","overlap":{"unit":"char","default":{"size":1,"unit":"sentence"}}}]}`)
	if err != nil {
		t.Fatal(err)
	}
	explanation, err := e.Explain(p)
	if err != nil || !strings.Contains(explanation, "default_unit=sentence") {
		t.Fatal(explanation, err)
	}
}
