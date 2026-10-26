package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"time"
)

func parseStatsDate(value string, end bool) (time.Time, error) {
	for _, layout := range []string{time.RFC3339, "2006-01-02 15:04:05", "2006-01-02"} {
		if parsed, err := time.Parse(layout, value); err == nil {
			if layout == "2006-01-02" && end {
				parsed = parsed.AddDate(0, 0, 1)
			}
			return parsed, nil
		}
	}
	_, err := time.Parse(time.RFC3339, value)
	return time.Time{}, err
}
func (h *SystemHandler) GetStats(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	for _, key := range []string{"from_date", "to_date", "canvas_id"} {
		if values, ok := c.Request.URL.Query()[key]; ok && (len(values) != 1 || values[0] == "") {
			jsonError(c, common.CodeArgumentError, "Invalid stats query.")
			return
		}
	}
	now := time.Now().UTC()
	from, err := parseStatsDate(c.DefaultQuery("from_date", now.AddDate(0, 0, -7).Format("2006-01-02")), false)
	if err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid from_date.")
		return
	}
	to, err := parseStatsDate(c.DefaultQuery("to_date", now.Format(time.RFC3339)), true)
	if err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid to_date.")
		return
	}
	var canvas *string
	if value, ok := c.GetQuery("canvas_id"); ok {
		canvas = &value
	}
	result, code, err := h.systemService.GetStats(c.Request.Context(), user.ID, from, to, canvas)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, result, "success")
}
