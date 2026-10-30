package handler

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
)

// bindExactObjectJSON also rejects case-folded aliases such as ids/IDS. The Go
// struct decoder otherwise accepts both and silently overwrites the first value.
func bindExactObjectJSON(c *gin.Context, target any, fields ...string) error {
	var object map[string]json.RawMessage
	if err := bindObjectJSON(c, &object); err != nil {
		return err
	}
	allowed := make(map[string]bool, len(fields))
	for _, field := range fields {
		allowed[field] = true
	}
	for field := range object {
		if !allowed[field] {
			return errors.New("Unknown request field.")
		}
	}
	raw, err := json.Marshal(object)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, target)
}

func (h *AgentHandler) DeleteAgentSessions(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.DeleteAgentSessionsRequest
	if err := bindExactObjectJSON(c, &req, "ids", "delete_all"); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid session deletion request.")
		return
	}
	result, code, err := h.agentService.DeleteAgentSessions(c.Request.Context(), user.ID, c.Param("agent_id"), req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	response := gin.H{"code": common.CodeSuccess}
	if result.Data != nil {
		response["data"] = result.Data
	}
	if result.Message != "" {
		response["message"] = result.Message
	}
	c.JSON(http.StatusOK, response)
}

func (h *AgentHandler) TestDBConnection(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.TestDBConnectionRequest
	if err := bindExactObjectJSON(c, &req, "db_type", "database", "username", "host", "port", "password"); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid database connection request.")
		return
	}
	code, err := h.agentService.TestDBConnection(c.Request.Context(), user.ID, req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": common.CodeSuccess, "message": "success"})
}
