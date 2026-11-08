package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
)

// Agent execution uses a strict body so unsupported Python session options
// cannot silently change meaning when a caller explicitly selects Go.
func bindAgentRequest(c *gin.Context, target any, fields ...string) error {
	var object map[string]json.RawMessage
	if err := bindExactObjectJSON(c, &object, fields...); err != nil {
		return err
	}
	raw, err := json.Marshal(object)
	if err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	d.DisallowUnknownFields()
	return d.Decode(target)
}

func (h *AgentHandler) CreateAgent(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.SaveAgentRequest
	if err := bindAgentRequest(c, &req, "title", "description", "permission", "canvas_type", "dsl"); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid agent request.")
		return
	}
	row, code, err := h.agentService.CreateAgent(c.Request.Context(), user.ID, &req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, row, "success")
}
func (h *AgentHandler) GetAgent(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	row, code, err := h.agentService.GetAgent(c.Request.Context(), user.ID, c.Param("agent_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, row, "success")
}
func (h *AgentHandler) UpdateAgent(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.SaveAgentRequest
	if err := bindAgentRequest(c, &req, "title", "description", "permission", "canvas_type", "dsl"); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid agent request.")
		return
	}
	code, err := h.agentService.UpdateAgent(c.Request.Context(), user.ID, c.Param("agent_id"), &req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, true, "success")
}
func (h *AgentHandler) DeleteAgent(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	code, err := h.agentService.DeleteAgent(c.Request.Context(), user.ID, c.Param("agent_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, true, "success")
}
func (h *AgentHandler) PublishAgent(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	if c.Request.ContentLength != 0 {
		var req struct{}
		if err := bindAgentRequest(c, &req); err != nil {
			jsonError(c, common.CodeArgumentError, "Publish accepts the saved draft only.")
			return
		}
	}
	row, code, err := h.agentService.PublishAgent(c.Request.Context(), user.ID, c.Param("agent_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, row, "success")
}
func (h *AgentHandler) DeleteAgentVersion(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	code, err := h.agentService.DeleteVersion(c.Request.Context(), user.ID, c.Param("agent_id"), c.Param("version_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, true, "success")
}
func (h *AgentHandler) RunAgent(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	if c.Request.URL.RawQuery != "" {
		jsonError(c, common.CodeArgumentError, "Go run options belong in the JSON body.")
		return
	}
	var req service.AgentRunRequest
	if err := bindAgentRequest(c, &req, "query", "inputs", "version_id", "run_id", "interrupt_id", "resume"); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid Go agent run request.")
		return
	}
	runCtx, cancel := context.WithCancel(c.Request.Context())
	defer cancel()
	events, code, err := h.agentService.RunAgent(runCtx, user.ID, c.Param("agent_id"), &req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache, no-store")
	c.Header("X-Accel-Buffering", "no")
	controller := http.NewResponseController(c.Writer)
	for event := range events {
		if err := controller.SetWriteDeadline(time.Now().Add(10 * time.Second)); err != nil && err != http.ErrNotSupported {
			return
		}
		raw, err := json.Marshal(event)
		if err != nil {
			return
		}
		if _, err = c.Writer.Write(append(append([]byte("data: "), raw...), []byte("\n\n")...)); err != nil {
			return
		}
		if err = controller.Flush(); err != nil {
			return
		}
	}
}
func (h *AgentHandler) CancelAgent(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req struct {
		RunID string `json:"run_id"`
	}
	if err := bindAgentRequest(c, &req, "run_id"); err != nil {
		jsonError(c, common.CodeArgumentError, "run_id is required.")
		return
	}
	code, err := h.agentService.CancelAgent(c.Request.Context(), user.ID, c.Param("agent_id"), req.RunID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, true, "success")
}
