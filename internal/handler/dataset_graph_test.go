package handler

import (
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
)

func TestKnowledgeGraphSortLimitsAndReferences(t *testing.T) {
	nodes := []interface{}{}
	edges := []interface{}{}
	for i := range 300 {
		nodes = append(nodes, map[string]interface{}{"id": fmt.Sprint(i), "pagerank": json.Number(fmt.Sprint(i))})
		if i > 0 {
			edges = append(edges, map[string]interface{}{"source": fmt.Sprint(i - 1), "target": fmt.Sprint(i), "weight": i})
		}
	}
	edges = append(edges, map[string]interface{}{"source": "299", "target": "299", "weight": 1000}, map[string]interface{}{"source": "299", "target": "missing", "weight": 1000})
	graph := map[string]interface{}{"nodes": nodes, "edges": edges}
	sortKnowledgeGraph(graph)
	sorted := graph["nodes"].([]map[string]interface{})
	links := graph["edges"].([]map[string]interface{})
	if len(sorted) != 256 || len(links) != 128 || sorted[0]["id"] != "299" || links[0]["weight"] != 299 {
		t.Fatal(len(sorted), len(links), sorted[0], links[0])
	}
	keep := map[string]bool{}
	for _, n := range sorted {
		keep[n["id"].(string)] = true
	}
	for _, e := range links {
		if !keep[e["source"].(string)] || !keep[e["target"].(string)] || e["source"] == e["target"] {
			t.Fatal(e)
		}
	}
}
func TestKnowledgeGraphRequiresIdentityBeforeEngineAccess(t *testing.T) {
	for _, remove := range []bool{false, true} {
		w := httptest.NewRecorder()
		context, _ := gin.CreateTestContext(w)
		context.Request = httptest.NewRequest("GET", "/api/v1/datasets/owned/graph", nil)
		handler := &DatasetsHandler{}
		if remove {
			handler.DeleteKnowledgeGraph(context)
		} else {
			handler.GetKnowledgeGraph(context)
		}
		var result struct{ Code common.ErrorCode }
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || result.Code != common.CodeUnauthorized {
			t.Fatal("identity rejected before nil service/global engine access", w.Body.String(), err)
		}
	}
}
