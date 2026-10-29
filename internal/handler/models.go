package handler

import (
	"fmt"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"multirag/internal/common"
	"multirag/internal/service"

	"github.com/gin-gonic/gin"
)

// ModelHandler serves static metadata. Both routers must mount it behind their
// existing authentication middleware; it never resolves tenant model bindings.
type ModelHandler struct{ modelProviderService *service.ModelProviderService }

func NewModelHandler(models *service.ModelProviderService) *ModelHandler {
	return &ModelHandler{models}
}

func catalogQuery(c *gin.Context, allowed ...string) (url.Values, error) {
	query, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil {
		return nil, fmt.Errorf("invalid query encoding")
	}
	for key, values := range query {
		valid := false
		for _, name := range allowed {
			valid = valid || key == name
		}
		if !valid || len(values) != 1 || values[0] == "" {
			return nil, fmt.Errorf("invalid catalog query")
		}
	}
	return query, nil
}

func (h *ModelHandler) ListAllModels(c *gin.Context) {
	if user, code, _ := GetUser(c); code != common.CodeSuccess || user == nil {
		c.JSON(http.StatusUnauthorized, gin.H{"code": common.CodeUnauthorized, "message": "Authentication required"})
		return
	}
	query, err := catalogQuery(c, "page", "page_size")
	page, size := 0, 0
	if err == nil {
		for _, field := range []struct {
			name   string
			target *int
		}{{"page", &page}, {"page_size", &size}} {
			if raw, ok := query[field.name]; ok {
				value, e := strconv.Atoi(raw[0])
				if e != nil || value < 0 {
					err = fmt.Errorf("invalid pagination")
					break
				}
				*field.target = value
			}
		}
	}
	if err != nil || (size == 0 && page != 0) || (size > 0 && page > math.MaxInt/size) {
		jsonError(c, common.CodeBadRequest, "Invalid catalog pagination")
		return
	}
	models, total, err := h.modelProviderService.ListAllModels(page, size)
	if err != nil {
		jsonError(c, common.CodeServerError, "Model catalog unavailable")
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": common.CodeSuccess, "data": models, "total": total, "message": "success"})
}

func (h *ModelHandler) ShowModel(c *gin.Context) {
	if user, code, _ := GetUser(c); code != common.CodeSuccess || user == nil {
		c.JSON(http.StatusUnauthorized, gin.H{"code": common.CodeUnauthorized, "message": "Authentication required"})
		return
	}
	query, err := catalogQuery(c, "model_name")
	if err != nil || strings.TrimSpace(query.Get("model_name")) == "" {
		jsonError(c, common.CodeBadRequest, "Model name required")
		return
	}
	model, err := h.modelProviderService.ShowModel(query.Get("model_name"))
	if err != nil {
		jsonError(c, common.CodeNotFound, "Model not found")
		return
	}
	jsonResponse(c, common.CodeSuccess, model, "success")
}
