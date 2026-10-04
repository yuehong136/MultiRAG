package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
	"net/http"
)

func (h *ProviderHandler) AddCustomModel(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		c.JSON(http.StatusUnauthorized, gin.H{"code": code, "message": message})
		return
	}
	var request service.AddCustomModelRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		jsonError(c, common.CodeBadRequest, "Invalid model declaration")
		return
	}
	provider, instance := c.Param("provider_name"), c.Param("instance_name")
	if (request.ProviderName != "" && request.ProviderName != provider) || (request.InstanceName != "" && request.InstanceName != instance) {
		jsonError(c, common.CodeBadRequest, "Model declaration does not match route")
		return
	}
	request.ProviderName, request.InstanceName = provider, instance
	code, err := h.modelProviderService.AddCustomModel(&request, user.ID)
	if err != nil {
		message := err.Error()
		if code == common.CodeServerError {
			message = "Unable to add custom model"
		}
		jsonError(c, code, message)
		return
	}
	jsonResponse(c, common.CodeSuccess, nil, "success")
}
