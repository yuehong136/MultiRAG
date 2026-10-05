//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package handler

import (
	"fmt"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity/models"
	"multirag/internal/service"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// ProviderHandler provider handler
type ProviderHandler struct {
	userService          *service.UserService
	modelProviderService *service.ModelProviderService
	userTenantDAO        *dao.UserTenantDAO
}

// NewProviderHandler create provider handler
func NewProviderHandler(userService *service.UserService, modelProviderService *service.ModelProviderService) *ProviderHandler {
	return &ProviderHandler{
		userService:          userService,
		modelProviderService: modelProviderService,
		userTenantDAO:        dao.NewUserTenantDAO(),
	}
}

func (h *ProviderHandler) ListProviders(c *gin.Context) {

	keywords := ""
	if queryKeywords := c.Query("available"); queryKeywords != "" {
		keywords = queryKeywords
	}

	// convert keywords to small case
	keywords = strings.ToLower(keywords)
	if keywords == "true" {
		// list pool providers
		providers, err := dao.GetModelProviderManager().ListProviders()
		if err != nil {
			c.JSON(http.StatusOK, gin.H{
				"code":    common.CodeNotFound,
				"message": err.Error(),
			})
			return
		}

		for _, provider := range providers {
			delete(provider, "url_suffix")
			delete(provider, "tags")
		}

		c.JSON(http.StatusOK, gin.H{
			"code":    0,
			"message": "success",
			"data":    providers,
		})
		return
	}

	userID := c.GetString("user_id")

	// list tenant providers
	providers, errorCode, err := h.modelProviderService.ListProvidersOfTenant(userID)
	if err != nil || errorCode != common.CodeSuccess {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": err.Error(),
			"data":    nil,
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    providers,
	})
	return
}

type AddProviderRequest struct {
	ProviderName string `json:"provider_name" binding:"required"`
}

func (h *ProviderHandler) AddProvider(c *gin.Context) {

	var req AddProviderRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeBadRequest,
			"message": err.Error(),
			"data":    false,
		})
		return
	}

	userID := c.GetString("user_id")

	errorCode, err := h.modelProviderService.AddModelProvider(req.ProviderName, userID)
	if err != nil || errorCode != common.CodeSuccess {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
	})
}

func (h *ProviderHandler) DeleteProvider(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	userID := c.GetString("user_id")

	errorCode, err := h.modelProviderService.DeleteModelProvider(providerName, userID)
	if err != nil || errorCode != common.CodeSuccess {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
	})
}

func (h *ProviderHandler) ShowProvider(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	provider, err := dao.GetModelProviderManager().GetProviderByName(providerName)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeNotFound,
			"message": err.Error(),
		})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    provider,
	})
}

func (h *ProviderHandler) ListModels(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}
	models, err := dao.GetModelProviderManager().ListModels(providerName)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeNotFound,
			"message": err.Error(),
		})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    models,
	})
}

func (h *ProviderHandler) ShowModel(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}
	modelName := c.Param("model_name")
	if modelName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Model name is required",
		})
		return
	}
	model, err := dao.GetModelProviderManager().GetModelByName(providerName, modelName)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeNotFound,
			"message": err.Error(),
		})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    model,
	})
}

type CreateProviderInstanceRequest struct {
	InstanceName string `json:"instance_name" binding:"required"`
	APIKey       string `json:"api_key"`
	BaseURL      string `json:"base_url"`
	Region       string `json:"region"`
}

func (h *ProviderHandler) CreateProviderInstance(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	var req CreateProviderInstanceRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeBadRequest,
			"message": err.Error(),
		})
		return
	}

	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		c.JSON(http.StatusUnauthorized, gin.H{"code": code, "message": message})
		return
	}
	userID := user.ID

	errorCode, err := h.modelProviderService.CreateProviderInstance(providerName, req.InstanceName, req.APIKey, userID, req.Region, req.BaseURL)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
	})
}

func (h *ProviderHandler) ListProviderInstances(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	userID := c.GetString("user_id")

	instances, errorCode, err := h.modelProviderService.ListProviderInstances(providerName, userID)
	if err != nil || errorCode != common.CodeSuccess {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    instances,
	})
}

func (h *ProviderHandler) ShowProviderInstance(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	instanceName := c.Param("instance_name")
	if instanceName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Instance name is required",
		})
		return
	}

	userID := c.GetString("user_id")

	// Get tenant ID from user
	instance, errorCode, err := h.modelProviderService.ShowProviderInstance(providerName, instanceName, userID)
	if err != nil || errorCode != common.CodeSuccess {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    instance,
	})
}

func (h *ProviderHandler) ShowInstanceBalance(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	instanceName := c.Param("instance_name")
	if instanceName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Instance name is required",
		})
		return
	}

	userID := c.GetString("user_id")

	balance, errorCode, err := h.modelProviderService.ShowInstanceBalance(providerName, instanceName, userID)
	if err != nil || errorCode != common.CodeSuccess {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    balance,
	})
}

func (h *ProviderHandler) CheckProviderConnection(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		jsonError(c, common.CodeArgumentError, "Provider name is required")
		return
	}
	instanceName := c.Param("instance_name")
	if instanceName == "" {
		jsonError(c, common.CodeArgumentError, "Instance name is required")
		return
	}
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}
	if errorCode, err := h.modelProviderService.CheckProviderConnection(providerName, instanceName, user.ID); err != nil {
		jsonError(c, errorCode, "Provider connection failed")
		return
	}
	jsonResponse(c, common.CodeSuccess, nil, "success")
}

type AlterProviderInstanceRequest struct {
	LLMName string `json:"llm_name" binding:"required"`
}

func (h *ProviderHandler) AlterProviderInstance(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	instanceName := c.Param("instance_name")
	if instanceName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Instance name is required",
		})
		return
	}

	var req AlterProviderInstanceRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeBadRequest,
			"message": err.Error(),
		})
		return
	}

	userID := c.GetString("user_id")
	if userID == "" {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeUnauthorized,
			"message": "Unauthorized",
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    common.CodeNotFound,
		"message": "success",
	})
}

type DropProviderInstanceRequest struct {
	Instances []string `json:"instances" binding:"required"`
}

func (h *ProviderHandler) DropProviderInstance(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		c.JSON(http.StatusUnauthorized, gin.H{"code": code, "message": message})
		return
	}
	var req DropProviderInstanceRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		jsonError(c, common.CodeBadRequest, "Invalid instance deletion request")
		return
	}
	code, err := h.modelProviderService.DropProviderInstances(c.Param("provider_name"), user.ID, req.Instances)
	if err != nil {
		message := err.Error()
		if code == common.CodeServerError {
			message = "Unable to delete provider instances"
		}
		jsonError(c, code, message)
		return
	}
	jsonResponse(c, common.CodeSuccess, nil, "success")
}

func (h *ProviderHandler) ListInstanceModels(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}
	instanceName := c.Param("instance_name")
	if instanceName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Instance name is required",
		})
		return
	}
	if strings.EqualFold(c.Query("supported"), "true") {
		modelNames, err := h.modelProviderService.ListSupportedModels(providerName, instanceName, c.GetString("user_id"))
		if err != nil {
			c.JSON(http.StatusOK, gin.H{"code": common.CodeServerError, "message": "Provider model listing failed"})
			return
		}
		models := make([]map[string]string, 0, len(modelNames))
		for _, modelName := range modelNames {
			models = append(models, map[string]string{"model_name": modelName})
		}
		c.JSON(http.StatusOK, gin.H{"code": 0, "message": "success", "data": models})
		return
	}
	models, err := h.modelProviderService.ListInstanceModels(providerName, instanceName, c.GetString("user_id"))
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeNotFound,
			"message": err.Error(),
		})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    models,
	})
}

type EnableOrDisableModelRequest struct {
	Status string `json:"status" binding:"required"`
}

func (h *ProviderHandler) EnableOrDisableModel(c *gin.Context) {
	providerName := c.Param("provider_name")
	if providerName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Provider name is required",
		})
		return
	}

	instanceName := c.Param("instance_name")
	if instanceName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Instance name is required",
		})
		return
	}

	modelName := strings.TrimPrefix(c.Param("model_name"), "/")
	if modelName == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "Model name is required",
		})
		return
	}

	var req EnableOrDisableModelRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		println("JSON bind error: %v (type: %T)", err, err)
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeBadRequest,
			"message": err.Error(),
		})
		return
	}

	userID := c.GetString("user_id")

	_, err := h.modelProviderService.UpdateModelStatus(providerName, instanceName, modelName, userID, req.Status)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    common.CodeServerError,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
	})
}

type ChatToModelRequest struct {
	ProviderName string           `json:"provider_name" binding:"required"`
	InstanceName string           `json:"instance_name" binding:"required"`
	ModelName    string           `json:"model_name" binding:"required"`
	Message      *string          `json:"message"`
	Messages     []models.Message `json:"messages"`
	Stream       *bool            `json:"stream"`
	Thinking     *bool            `json:"thinking"`
	Effort       *string          `json:"effort"`
	Verbosity    *string          `json:"verbosity"`
}

func (h *ProviderHandler) ChatToModel(c *gin.Context) {
	var req ChatToModelRequest
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.ProviderName) == "" || strings.TrimSpace(req.InstanceName) == "" || strings.TrimSpace(req.ModelName) == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    common.CodeBadRequest,
			"message": "Provider, instance, model and message are required",
		})
		return
	}
	messages, err := req.chatMessages()
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "message": err.Error()})
		return
	}
	providerName, instanceName := req.ProviderName, req.InstanceName

	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		c.JSON(http.StatusUnauthorized, gin.H{"code": code, "message": message})
		return
	}

	if req.Thinking != nil && !*req.Thinking {
		req.Effort = nil
		req.Verbosity = nil
	}

	apiConfig := models.APIConfig{Context: c.Request.Context()}
	chatConfig := models.ChatConfig{
		Thinking:  req.Thinking,
		Stream:    req.Stream,
		Stop:      &[]string{},
		Effort:    req.Effort,
		Verbosity: req.Verbosity,
	}

	// Check if it's a stream request
	if req.Stream != nil && *req.Stream {
		// Set SSE headers
		c.Header("Content-Type", "text/event-stream")
		c.Header("Cache-Control", "no-cache")
		c.Header("Connection", "keep-alive")
		c.Writer.WriteHeader(http.StatusOK)
		c.Writer.Flush()

		// Create sender function that writes directly to response
		sender := func(content, reasoningContent *string) error {
			if err := c.Request.Context().Err(); err != nil {
				return err
			}
			// Check for [DONE] marker (OpenAI compatible)
			if content != nil {
				if *content == "[DONE]" {
					c.SSEvent("done", "[DONE]")
					c.Writer.Flush()
					if len(c.Errors) > 0 {
						return c.Errors.Last()
					}
					return nil
				}
				message := fmt.Sprintf("[MESSAGE]%s", *content)
				c.SSEvent("message", message)
				c.Writer.Flush()
			}

			if reasoningContent != nil {
				message := fmt.Sprintf("[REASONING]%s", *reasoningContent)
				c.SSEvent("message", message)
				c.Writer.Flush()
			}

			if len(c.Errors) > 0 {
				return c.Errors.Last()
			}
			return nil
		}

		// Stream response using sender function (best performance, no channel)
		errorCode, err := h.modelProviderService.ChatToModelStreamWithMessages(providerName, instanceName, req.ModelName, user.ID, messages, &apiConfig, &chatConfig, sender)

		if errorCode != common.CodeSuccess || err != nil {
			c.SSEvent("error", "Model stream failed")
			c.Writer.Flush()
		}
		return
	}

	// Non-stream response
	response, errorCode, err := h.modelProviderService.ChatToModelWithMessages(providerName, instanceName, req.ModelName, user.ID, messages, &apiConfig, &chatConfig)
	if err != nil || errorCode != common.CodeSuccess {
		c.JSON(http.StatusOK, gin.H{
			"code":    errorCode,
			"message": "Model request failed",
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":              0,
		"reasoning_content": response.ReasoningContent,
		"answer":            response.Answer,
	})
}

// chatMessages keeps the legacy single-message API and rejects ambiguous dual input.
func (r *ChatToModelRequest) chatMessages() ([]models.Message, error) {
	messages := r.Messages
	if r.Message != nil {
		if messages != nil {
			return nil, fmt.Errorf("send either message or messages")
		}
		messages = []models.Message{{Role: "user", Content: *r.Message}}
	}
	if err := models.ValidateMessages(messages); err != nil {
		return nil, err
	}
	return messages, nil
}
