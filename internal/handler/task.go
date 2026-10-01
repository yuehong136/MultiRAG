package handler

import (
	"encoding/json"
	"errors"
	"io"
	"regexp"

	"multirag/internal/common"
	"multirag/internal/service"

	"github.com/gin-gonic/gin"
)

type TaskHandler struct {
	service *service.TaskService
}

func NewTaskHandler(taskService *service.TaskService) *TaskHandler {
	return &TaskHandler{service: taskService}
}

var taskIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,32}$`)

func (h *TaskHandler) Cancel(c *gin.Context) {
	h.cancel(c)
}

func (h *TaskHandler) Update(c *gin.Context) {
	var body struct {
		Action string `json:"action"`
	}
	decoder := json.NewDecoder(c.Request.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil || body.Action != "stop" {
		jsonError(c, common.CodeParamError, "action must be stop")
		return
	}
	if err := decoder.Decode(new(interface{})); !errors.Is(err, io.EOF) {
		jsonError(c, common.CodeParamError, "invalid request body")
		return
	}
	h.cancel(c)
}

func (h *TaskHandler) cancel(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	taskID := c.Param("task_id")
	if !taskIDPattern.MatchString(taskID) {
		jsonError(c, common.CodeParamError, "invalid task id")
		return
	}
	if err := h.service.Cancel(c.Request.Context(), taskID, user.ID); err != nil {
		if errors.Is(err, service.ErrTaskForbidden) {
			jsonResponse(c, common.CodeAuthenticationError, false, err.Error())
		} else {
			jsonResponse(c, common.CodeExceptionError, false, "Failed to submit task cancellation.")
		}
		return
	}
	jsonResponse(c, common.CodeSuccess, true, "success")
}
