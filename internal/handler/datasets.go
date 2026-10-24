//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package handler

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/service"
)

// DatasetsHandler handles the RESTful dataset endpoints.
type DatasetsHandler struct {
	datasetsService *service.DatasetsService
	documentEngine  func() engine.DocEngine
}

type listDatasetsExt struct {
	Keywords string   `json:"keywords,omitempty"`
	OwnerIDs []string `json:"owner_ids,omitempty"`
	ParserID string   `json:"parser_id,omitempty"`
}

// NewDatasetsHandler creates a new datasets handler.
func NewDatasetsHandler(datasetsService *service.DatasetsService, documentEngines ...engine.DocEngine) *DatasetsHandler {
	getter := engine.Get
	if len(documentEngines) > 0 {
		getter = func() engine.DocEngine { return documentEngines[0] }
	}
	return &DatasetsHandler{datasetsService: datasetsService, documentEngine: getter}
}

// ListDatasets handles GET /api/v1/datasets.
func (h *DatasetsHandler) ListDatasets(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	page := 1
	if pageStr := c.Query("page"); pageStr != "" {
		if p, err := strconv.Atoi(pageStr); err == nil && p > 0 {
			page = p
		}
	}

	pageSize := 30
	if pageSizeStr := c.Query("page_size"); pageSizeStr != "" {
		if ps, err := strconv.Atoi(pageSizeStr); err == nil && ps > 0 {
			pageSize = ps
		}
	}

	orderby := "create_time"
	if queryOrderby := c.Query("orderby"); queryOrderby != "" {
		orderby = queryOrderby
	}

	desc := true
	if descStr := c.Query("desc"); descStr != "" {
		desc = strings.ToLower(descStr) == "true"
	}

	keywords := ""
	parserID := ""
	var ownerIDs []string

	// ext keeps the same compatibility payload as the Python REST API.
	if extStr := c.Query("ext"); extStr != "" {
		var ext listDatasetsExt
		if err := json.Unmarshal([]byte(extStr), &ext); err != nil {
			jsonError(c, common.CodeDataError, err.Error())
			return
		}
		keywords = ext.Keywords
		parserID = ext.ParserID
		ownerIDs = ext.OwnerIDs
	}

	data, total, code, err := h.datasetsService.ListDatasets(
		c.Query("id"),
		c.Query("name"),
		page,
		pageSize,
		orderby,
		desc,
		keywords,
		ownerIDs,
		parserID,
		user.ID,
	)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":           common.CodeSuccess,
		"data":           data,
		"total_datasets": total,
	})
}

// CreateDataset handles POST /api/v1/datasets.
func (h *DatasetsHandler) CreateDataset(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	var req service.CreateDatasetRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		jsonError(c, common.CodeDataError, err.Error())
		return
	}

	result, code, err := h.datasetsService.CreateDataset(&req, user.ID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code": common.CodeSuccess,
		"data": result,
	})
}

// DeleteDatasets handles DELETE /api/v1/datasets.
func (h *DatasetsHandler) DeleteDatasets(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	var req struct {
		IDs       *[]string `json:"ids"`
		DeleteAll bool      `json:"delete_all,omitempty"`
	}
	if c.Request.ContentLength > 0 {
		if err := c.ShouldBindJSON(&req); err != nil {
			jsonError(c, common.CodeDataError, err.Error())
			return
		}
	}

	var ids []string
	if req.IDs != nil {
		ids = *req.IDs
	}

	result, code, err := h.datasetsService.DeleteDatasets(ids, req.DeleteAll, user.ID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code": common.CodeSuccess,
		"data": result,
	})
}

// GetDataset handles GET /api/v1/datasets/:dataset_id.
func (h *DatasetsHandler) GetDataset(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	data, code, err := h.datasetsService.GetDataset(c.Request.Context(), c.Param("dataset_id"), user.ID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": common.CodeSuccess, "data": data})
}

// GetKnowledgeGraph handles GET /api/v1/datasets/:dataset_id/graph.
func (h *DatasetsHandler) GetKnowledgeGraph(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	datasetID := strings.TrimSpace(c.Param("dataset_id"))
	if datasetID == "" {
		jsonError(c, common.CodeDataError, "dataset_id is required")
		return
	}

	dataset, code, err := h.datasetsService.GetDataset(c.Request.Context(), datasetID, user.ID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}

	canonicalID, _ := dataset["id"].(string)
	if canonicalID == "" {
		jsonError(c, common.CodeServerError, "authorized dataset identity is missing")
		return
	}
	datasetID = canonicalID
	tenantID, _ := dataset["tenant_id"].(string)
	if tenantID == "" {
		jsonError(c, common.CodeDataError, "tenant_id is required")
		return
	}

	if strings.TrimSpace(c.Query("doc_id")) != "" {
		jsonError(c, common.CodeBadRequest, "document subgraphs are unavailable in this Go engine")
		return
	}
	docEngine := h.documentEngine()
	if docEngine == nil {
		jsonError(c, common.CodeServerError, "Document engine is not initialized")
		return
	}

	indexName := fmt.Sprintf("multirag_%s", tenantID)
	exists, err := docEngine.ChunkStoreExists(c.Request.Context(), indexName, datasetID)
	if err != nil {
		jsonError(c, common.CodeServerError, err.Error())
		return
	}

	result := gin.H{
		"graph":    map[string]interface{}{},
		"mind_map": map[string]interface{}{},
	}
	if !exists {
		jsonResponse(c, common.CodeSuccess, result, "success")
		return
	}

	searchResult, err := docEngine.Search(c.Request.Context(), &types.SearchRequest{
		IndexNames:   []string{indexName},
		KbIDs:        []string{datasetID},
		Offset:       0,
		Limit:        2,
		SelectFields: []string{"content_with_weight", "knowledge_graph_kwd", "kb_id"},
		Filter: map[string]interface{}{
			"kb_id":               []string{datasetID},
			"knowledge_graph_kwd": []string{"graph"},
		},
	})
	if err != nil {
		jsonError(c, common.CodeServerError, err.Error())
		return
	}
	if searchResult == nil {
		jsonError(c, common.CodeServerError, "invalid graph search response")
		return
	}
	if len(searchResult.Chunks) == 0 {
		jsonResponse(c, common.CodeSuccess, result, "success")
		return
	}

	if len(searchResult.Chunks) != 1 {
		jsonError(c, common.CodeServerError, "duplicate dataset graph records")
		return
	}
	chunk := searchResult.Chunks[0]
	if !graphChunkBelongs(chunk["kb_id"], datasetID) || firstStringValue(chunk["knowledge_graph_kwd"]) != "graph" {
		jsonError(c, common.CodeServerError, "graph query returned an invalid scope")
		return
	}
	graphType := firstStringValue(chunk["knowledge_graph_kwd"])
	contentWithWeight, _ := chunk["content_with_weight"].(string)
	if strings.TrimSpace(contentWithWeight) == "" {
		jsonError(c, common.CodeServerError, "stored graph content is empty")
		return
	}
	var graphData map[string]interface{}
	decoder := json.NewDecoder(strings.NewReader(contentWithWeight))
	decoder.UseNumber()
	if err := decoder.Decode(&graphData); err != nil || graphData == nil {
		jsonError(c, common.CodeServerError, "stored graph must be a JSON object")
		return
	}
	var trailing interface{}
	if err := decoder.Decode(&trailing); err != io.EOF {
		jsonError(c, common.CodeServerError, "stored graph has trailing data")
		return
	}
	if err := sortKnowledgeGraph(graphData); err != nil {
		jsonError(c, common.CodeServerError, err.Error())
		return
	}
	result[graphType] = graphData

	jsonResponse(c, common.CodeSuccess, result, "success")
}

// DeleteKnowledgeGraph handles DELETE /api/v1/datasets/:dataset_id/graph.
func (h *DatasetsHandler) DeleteKnowledgeGraph(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	datasetID := strings.TrimSpace(c.Param("dataset_id"))
	if datasetID == "" {
		jsonError(c, common.CodeDataError, "dataset_id is required")
		return
	}

	dataset, code, err := h.datasetsService.GetDataset(c.Request.Context(), datasetID, user.ID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}

	canonicalID, _ := dataset["id"].(string)
	if canonicalID == "" {
		jsonError(c, common.CodeServerError, "authorized dataset identity is missing")
		return
	}
	datasetID = canonicalID
	tenantID, _ := dataset["tenant_id"].(string)
	if tenantID == "" {
		jsonError(c, common.CodeDataError, "tenant_id is required")
		return
	}

	if strings.TrimSpace(c.Query("doc_id")) != "" {
		jsonError(c, common.CodeBadRequest, "document subgraphs are unavailable in this Go engine")
		return
	}
	docEngine := h.documentEngine()
	if docEngine == nil {
		jsonError(c, common.CodeServerError, "Document engine is not initialized")
		return
	}

	indexName := fmt.Sprintf("multirag_%s", tenantID)
	if _, err := docEngine.DeleteChunks(c.Request.Context(), map[string]interface{}{
		"knowledge_graph_kwd": []string{"graph", "subgraph", "entity", "relation", "community_report"},
	}, indexName, datasetID); err != nil {
		jsonError(c, common.CodeServerError, err.Error())
		return
	}

	jsonResponse(c, common.CodeSuccess, true, "success")
}

func firstStringValue(value interface{}) string {
	switch v := value.(type) {
	case string:
		return strings.TrimSpace(v)
	case []string:
		if len(v) > 0 {
			return strings.TrimSpace(v[0])
		}
	case []interface{}:
		for _, item := range v {
			if s, ok := item.(string); ok {
				s = strings.TrimSpace(s)
				if s != "" {
					return s
				}
			}
		}
	}
	return ""
}

func sortKnowledgeGraph(graphData map[string]interface{}) error {
	nodes, err := graphMapSlice(graphData["nodes"])
	if err != nil {
		return err
	}
	ids := map[string]bool{}
	for _, node := range nodes {
		id, ok := node["id"].(string)
		if !ok || id == "" || ids[id] {
			return fmt.Errorf("graph contains an invalid or duplicate node id")
		}
		ids[id] = true
		if _, err := graphNumber(node["pagerank"]); err != nil {
			return err
		}
	}
	sort.SliceStable(nodes, func(i, j int) bool {
		a, _ := graphNumber(nodes[i]["pagerank"])
		b, _ := graphNumber(nodes[j]["pagerank"])
		return a > b
	})
	if len(nodes) > 256 {
		nodes = nodes[:256]
	}
	graphData["nodes"] = nodes
	kept := map[string]bool{}
	for _, node := range nodes {
		kept[node["id"].(string)] = true
	}
	edges, err := graphMapSlice(graphData["edges"])
	if err != nil {
		return err
	}
	filtered := make([]map[string]interface{}, 0, len(edges))
	for _, edge := range edges {
		source, _ := edge["source"].(string)
		target, _ := edge["target"].(string)
		if source == target || !kept[source] || !kept[target] {
			continue
		}
		if _, err := graphNumber(edge["weight"]); err != nil {
			return err
		}
		filtered = append(filtered, edge)
	}
	sort.SliceStable(filtered, func(i, j int) bool {
		a, _ := graphNumber(filtered[i]["weight"])
		b, _ := graphNumber(filtered[j]["weight"])
		return a > b
	})
	if len(filtered) > 128 {
		filtered = filtered[:128]
	}
	graphData["edges"] = filtered
	return nil
}
func graphMapSlice(value interface{}) ([]map[string]interface{}, error) {
	if value == nil {
		return []map[string]interface{}{}, nil
	}
	if typed, ok := value.([]map[string]interface{}); ok {
		return append([]map[string]interface{}(nil), typed...), nil
	}
	raw, ok := value.([]interface{})
	if !ok {
		return nil, fmt.Errorf("graph nodes and edges must be arrays")
	}
	result := make([]map[string]interface{}, 0, len(raw))
	for _, item := range raw {
		m, ok := item.(map[string]interface{})
		if !ok || m == nil {
			return nil, fmt.Errorf("graph entries must be objects")
		}
		result = append(result, m)
	}
	return result, nil
}
func graphNumber(value interface{}) (float64, error) {
	if value == nil {
		return 0, nil
	}
	var number float64
	switch v := value.(type) {
	case float64:
		number = v
	case float32:
		number = float64(v)
	case int:
		number = float64(v)
	case int64:
		number = float64(v)
	case json.Number:
		var err error
		number, err = v.Float64()
		if err != nil {
			return 0, fmt.Errorf("invalid graph weight")
		}
	default:
		return 0, fmt.Errorf("graph weights must be numeric")
	}
	if math.IsNaN(number) || math.IsInf(number, 0) {
		return 0, fmt.Errorf("graph weight is not finite")
	}
	return number, nil
}

func graphChunkBelongs(value interface{}, datasetID string) bool {
	switch v := value.(type) {
	case string:
		return v == datasetID
	case []string:
		return len(v) == 1 && v[0] == datasetID
	case []interface{}:
		return len(v) == 1 && v[0] == datasetID
	default:
		return false
	}
}
