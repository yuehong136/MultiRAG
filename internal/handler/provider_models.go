package handler

import (
	"fmt"
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
	"net/http"
	"strings"
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
	if err := prepareCustomModelRequest(&request, c.Param("provider_name"), c.Param("instance_name")); err != nil {
		jsonError(c, common.CodeBadRequest, "Invalid model target or declaration")
		return
	}
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

type DropInstanceModelsRequest struct {
	Models []string `json:"models" binding:"required"`
}

func (h *ProviderHandler) DropInstanceModels(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		c.JSON(http.StatusUnauthorized, gin.H{"code": code, "message": message})
		return
	}
	var req DropInstanceModelsRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		jsonError(c, common.CodeBadRequest, "Invalid model deletion request")
		return
	}
	code, err := h.modelProviderService.DropInstanceModels(c.Param("provider_name"), c.Param("instance_name"), user.ID, req.Models)
	if err != nil {
		message := err.Error()
		if code == common.CodeServerError {
			message = "Unable to delete instance models"
		}
		jsonError(c, code, message)
		return
	}
	jsonResponse(c, common.CodeSuccess, nil, "success")
}

func prepareCustomModelRequest(request *service.AddCustomModelRequest, provider, instance string) error {
	if request == nil || provider == "" || provider != strings.TrimSpace(provider) || instance == "" || instance != strings.TrimSpace(instance) {
		return fmt.Errorf("provider and instance are required")
	}
	if request.ProviderName != "" && request.ProviderName != provider || request.InstanceName != "" && request.InstanceName != instance {
		return fmt.Errorf("model declaration does not match route")
	}
	if request.ModelName == "" || request.ModelName != strings.TrimSpace(request.ModelName) || request.ModelTypes != nil && len(request.ModelTypes) == 0 || request.ModelTypes == nil && request.ModelType == "" {
		return fmt.Errorf("model name and type are required")
	}
	request.ProviderName, request.InstanceName = provider, instance
	return nil
}
