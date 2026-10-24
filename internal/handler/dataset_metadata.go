package handler

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
)

func (h *DatasetsHandler) ListMetadataFlattened(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	query := c.Request.URL.Query()["dataset_ids"]
	if len(query) != 1 {
		jsonError(c, common.CodeDataError, "one dataset_ids query parameter is required")
		return
	}
	result, code, err := h.datasetsService.FlattenMetadata(c.Request.Context(), strings.Split(query[0], ","), user.ID, h.documentEngine())
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}

func (h *DatasetsHandler) RemoveTags(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var request struct {
		Tags []string `json:"tags"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(c.Writer, c.Request.Body, 1024*1024))
	opening, err := decoder.Token()
	if err != nil || opening != json.Delim('{') {
		jsonError(c, common.CodeDataError, "one tags object is required")
		return
	}
	key, err := decoder.Token()
	if err != nil || key != "tags" || decoder.Decode(&request.Tags) != nil || decoder.More() || len(request.Tags) == 0 || len(request.Tags) > 100 {
		jsonError(c, common.CodeDataError, "tags must contain between 1 and 100 distinct non-empty strings")
		return
	}
	if closing, err := decoder.Token(); err != nil || closing != json.Delim('}') {
		jsonError(c, common.CodeDataError, "invalid tags object")
		return
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		jsonError(c, common.CodeDataError, "unexpected trailing JSON")
		return
	}
	seen := map[string]bool{}
	for _, tag := range request.Tags {
		if strings.TrimSpace(tag) == "" || seen[tag] {
			jsonError(c, common.CodeDataError, "tags must be distinct non-empty strings")
			return
		}
		seen[tag] = true
	}
	dataset, code, err := h.datasetsService.GetDataset(c.Request.Context(), c.Param("dataset_id"), user.ID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	id, _ := dataset["id"].(string)
	tenant, _ := dataset["tenant_id"].(string)
	if id == "" || tenant == "" {
		jsonError(c, common.CodeServerError, "authorized dataset identity is missing")
		return
	}
	store := h.documentEngine()
	if store == nil {
		jsonError(c, common.CodeServerError, "document engine is not initialized")
		return
	}
	// Validate the complete request before the first mutation. Storage errors remain visible.
	for _, tag := range request.Tags {
		if err := store.UpdateChunks(c.Request.Context(), map[string]any{"kb_id": id, "tag_kwd": tag}, map[string]any{"remove": map[string]any{"tag_kwd": tag}}, fmt.Sprintf("multirag_%s", tenant), id); err != nil {
			jsonError(c, common.CodeServerError, err.Error())
			return
		}
	}

	jsonResponse(c, common.CodeSuccess, true, "success")
}
