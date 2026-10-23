package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity/models"
)

type ParseFileRequest struct {
	mediaIdentity
	Content *string `json:"content"`
	URL     *string `json:"url"`
}

func (h *ProviderHandler) ParseFile(c *gin.Context) {
	var req ParseFileRequest
	user, ok := bindMedia(c, &req)
	if !ok {
		return
	}
	if (req.Content == nil) == (req.URL == nil) {
		jsonError(c, common.CodeBadRequest, "provide content or URL exclusively")
		return
	}
	var content []byte
	var documentURL string
	if req.Content != nil {
		var err error
		content, err = models.DecodeMediaData(*req.Content)
		if err != nil {
			jsonError(c, common.CodeBadRequest, err.Error())
			return
		}
	} else {
		documentURL = *req.URL
	}
	result, code, err := h.modelProviderService.ParseFile(req.ProviderName, req.InstanceName, req.ModelName, user, content, documentURL, &models.APIConfig{Context: c.Request.Context()})
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}

func (h *ProviderHandler) ListTasks(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	result, code, err := h.modelProviderService.ListProviderTasks(c.Param("provider_name"), c.Param("instance_name"), user.ID, &models.APIConfig{Context: c.Request.Context()})
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}

func (h *ProviderHandler) ShowTask(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	result, code, err := h.modelProviderService.ShowProviderTask(c.Param("provider_name"), c.Param("instance_name"), c.Param("task_id"), user.ID, &models.APIConfig{Context: c.Request.Context()})
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}
