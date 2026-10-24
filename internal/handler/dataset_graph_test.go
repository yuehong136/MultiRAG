package handler

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/engine"
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
	if err := sortKnowledgeGraph(graph); err != nil {
		t.Fatal(err)
	}
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
	if value, err := graphNumber(json.Number("0")); err != nil || value != 0 {
		t.Fatal(value, err)
	}
	for _, bad := range []interface{}{math.NaN(), math.Inf(1), "123", json.Number("broken")} {
		if _, err := graphNumber(bad); err == nil {
			t.Fatal(bad)
		}
	}
	for _, bad := range []map[string]interface{}{{"nodes": "bad"}, {"nodes": []interface{}{nil}}, {"nodes": []interface{}{map[string]interface{}{"id": "a"}, map[string]interface{}{"id": "a"}}}} {
		if err := sortKnowledgeGraph(bad); err == nil {
			t.Fatal(bad)
		}
	}
}
func TestKnowledgeGraphRequiresIdentityBeforeEngineAccess(t *testing.T) {
	for _, remove := range []bool{false, true} {
		context, _ := gin.CreateTestContext(httptest.NewRecorder())
		context.Request = httptest.NewRequest("GET", "/api/v1/datasets/owned/graph", nil)
		called := false
		handler := &DatasetsHandler{documentEngine: func() engine.DocEngine { called = true; return nil }}
		if remove {
			handler.DeleteKnowledgeGraph(context)
		} else {
			handler.GetKnowledgeGraph(context)
		}
		if called {
			t.Fatal("unauthenticated request touched engine")
		}
	}
}
