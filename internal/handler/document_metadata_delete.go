package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gin-gonic/gin"
	"io"
	"multirag/internal/common"
	"multirag/internal/engine/types"
	"multirag/internal/service"
	"net/http"
	"strings"
)

// decodeMetadataDeletion preserves omission versus explicit empty/null keys and
// rejects duplicate fields before any authorization query or storage operation.
func decodeMetadataDeletion(reader io.Reader) (string, []string, error) {
	decoder := json.NewDecoder(reader)
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return "", nil, fmt.Errorf("metadata deletion object required")
	}
	seen := map[string]bool{}
	docID := ""
	var keys []string
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return "", nil, err
		}
		key, ok := token.(string)
		if !ok || seen[key] {
			return "", nil, fmt.Errorf("duplicate metadata deletion field")
		}
		seen[key] = true
		switch key {
		case "doc_id":
			if err := decoder.Decode(&docID); err != nil {
				return "", nil, err
			}
		case "keys":
			// The management CLI sends a JSON array encoded as a string.
			var encoded string
			if err := decoder.Decode(&encoded); err != nil || encoded == "" {
				return "", nil, fmt.Errorf("keys must be a JSON array string")
			}
			if err := json.Unmarshal([]byte(encoded), &keys); err != nil {
				return "", nil, err
			}
			if err := types.ValidateMetadataKeys(keys); err != nil {
				return "", nil, err
			}
		default:
			return "", nil, fmt.Errorf("unknown metadata deletion field")
		}
	}
	if token, err := decoder.Token(); err != nil || token != json.Delim('}') {
		return "", nil, fmt.Errorf("invalid metadata deletion object")
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF || strings.TrimSpace(docID) == "" {
		return "", nil, fmt.Errorf("one non-empty document identity is required")
	}
	return docID, keys, nil
}

func (h *DocumentHandler) DeleteMeta(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	docID, keys, err := decodeMetadataDeletion(http.MaxBytesReader(c.Writer, c.Request.Body, 1024*1024))
	if err != nil {
		jsonError(c, common.CodeDataError, err.Error())
		return
	}
	if err := h.documentService.DeleteDocumentMetadata(c.Request.Context(), docID, user.ID, keys); err != nil {
		code := common.CodeServerError
		switch {
		case errors.Is(err, service.ErrDatasetAccessDenied):
			code = common.CodeForbidden
		case errors.Is(err, service.ErrChunkNotFound):
			code = common.CodeNotFound
		case errors.Is(err, service.ErrInvalidChunkScope):
			code = common.CodeDataError
		}
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, true, "success")
}
