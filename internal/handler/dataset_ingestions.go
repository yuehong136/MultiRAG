package handler

import (
	"errors"
	"fmt"
	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/service"
	"net/url"
	"strconv"
	"strings"
	"time"
)

func ingestionQuery(values url.Values) (dao.IngestionQuery, error) {
	q := dao.IngestionQuery{OrderBy: "create_time", LogType: "dataset", Desc: true}
	for _, key := range []string{"page", "page_size", "orderby", "desc", "log_type", "keywords", "create_date_from", "create_date_to"} {
		if v, ok := values[key]; ok && (len(v) != 1 || (v[0] == "" && key != "keywords")) {
			return q, fmt.Errorf("invalid %s", key)
		}
	}
	for key, target := range map[string]*int{"page": &q.Page, "page_size": &q.PageSize} {
		if v, ok := values[key]; ok {
			if strings.ContainsFunc(v[0], func(r rune) bool { return r < '0' || r > '9' }) {
				return q, fmt.Errorf("invalid %s", key)
			}
			n, err := strconv.Atoi(v[0])
			if err != nil {
				return q, fmt.Errorf("invalid %s", key)
			}
			*target = n
		}
	}
	if v, ok := values["orderby"]; ok {
		q.OrderBy = v[0]
	}
	if v, ok := values["log_type"]; ok {
		q.LogType = v[0]
	}
	q.Keywords = values.Get("keywords")
	if v, ok := values["desc"]; ok {
		if v[0] != "true" && v[0] != "false" && v[0] != "0" && v[0] != "1" {
			return q, fmt.Errorf("invalid desc")
		}
		q.Desc = v[0] == "true" || v[0] == "1"
	}
	for key, target := range map[string]**time.Time{"create_date_from": &q.From, "create_date_to": &q.To} {
		if v, ok := values[key]; ok {
			var parsed time.Time
			var err error
			for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05", "2006-01-02"} {
				parsed, err = time.Parse(layout, v[0])
				if err == nil {
					break
				}
			}
			if err != nil {
				return q, fmt.Errorf("invalid %s", key)
			}
			*target = &parsed
		}
	}
	q.Status = values["operation_status"]
	q.Types = values["types"]
	q.Suffix = values["suffix"]
	// Local writers use numeric TaskStatus; canonical names expand additively.
	if q.Status != nil {
		expanded := []string{}
		names := map[string]string{"UNSTART": "0", "RUNNING": "1", "CANCEL": "2", "DONE": "3", "FAIL": "4"}
		for _, value := range q.Status {
			numeric, ok := names[value]
			if !ok {
				numeric = value
			}
			if numeric != "0" && numeric != "1" && numeric != "2" && numeric != "3" && numeric != "4" {
				return q, fmt.Errorf("invalid operation_status")
			}
			expanded = append(expanded, numeric)
			for name, n := range names {
				if n == numeric {
					expanded = append(expanded, name)
				}
			}
		}
		q.Status = expanded
	}
	return q, q.Validate()
}
func ingestionResponse(c *gin.Context, data map[string]any, err error) {
	if err == nil {
		jsonResponse(c, common.CodeSuccess, data, "success")
		return
	}
	if errors.Is(err, service.ErrDatasetAccessDenied) || errors.Is(err, gorm.ErrRecordNotFound) {
		jsonError(c, common.CodeDataError, "Log or dataset is unavailable.")
		return
	}
	jsonError(c, common.CodeServerError, "Database operation failed")
}
func (h *DatasetsHandler) GetIngestionSummary(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	data, err := h.datasetsService.GetIngestionSummary(c.Request.Context(), c.Param("dataset_id"), user.ID)
	ingestionResponse(c, data, err)
}
func (h *DatasetsHandler) ListIngestionLogs(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	q, err := ingestionQuery(c.Request.URL.Query())
	if err != nil {
		jsonError(c, common.CodeArgumentError, err.Error())
		return
	}
	data, err := h.datasetsService.ListIngestionLogs(c.Request.Context(), c.Param("dataset_id"), user.ID, q)
	ingestionResponse(c, data, err)
}
func (h *DatasetsHandler) GetIngestionLog(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	data, err := h.datasetsService.GetIngestionLog(c.Request.Context(), c.Param("dataset_id"), user.ID, c.Param("log_id"))
	ingestionResponse(c, data, err)
}
