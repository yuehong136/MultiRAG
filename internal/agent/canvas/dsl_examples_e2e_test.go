package canvas

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// v1Examples lists the fixtures the e2e suite runs against. Keep this
// in sync with internal/agent/dsl/v1_examples_test.go:v1Examples.
var v1Examples = []string{
	"categorize_and_agent_with_tavily.json",
	"exesql.json",
	"headhunter_zh.json",
	"iteration.json",
	"retrieval_and_generate.json",
	"retrieval_categorize_and_generate.json",
	"tavily_and_generate.json",
}

// These examples document accepted v1 JSON shape only. Many contain unsupported
// retrieval, SQL, iteration or cyclic behavior; parsing is not execution evidence.
func TestDSLExamples_ParseAsCanvas(t *testing.T) {
	for _, name := range v1Examples {
		t.Run(name, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join("..", "dsl", "testdata", "v1_examples", name))
			if err != nil {
				t.Fatal(err)
			}
			var c Canvas
			if err := json.Unmarshal(raw, &c); err != nil {
				t.Fatal(err)
			}
			if len(c.Components) == 0 {
				t.Fatal("empty components")
			}
			for id, comp := range c.Components {
				if comp.Obj.ComponentName == "" {
					t.Fatalf("%s empty component_name", id)
				}
			}
		})
	}
}
