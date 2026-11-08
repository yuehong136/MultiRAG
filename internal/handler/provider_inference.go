package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity/models"
	"net/http"
)

type EmbedTextRequest struct {
	ModelID      *int64   `json:"model_id"`
	ProviderName string   `json:"provider_name"`
	InstanceName string   `json:"instance_name"`
	ModelName    string   `json:"model_name"`
	Texts        []string `json:"texts" binding:"required,min=1"`
	Dimension    int      `json:"dimension" binding:"gte=0"`
}

func (h *ProviderHandler) EmbedText(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		c.JSON(http.StatusUnauthorized, gin.H{"code": code, "message": message})
		return
	}
	var req EmbedTextRequest
	if err := c.ShouldBindJSON(&req); err != nil || validateModelSelection(req.ModelID, req.ProviderName, req.InstanceName, req.ModelName) != nil {
		jsonError(c, common.CodeBadRequest, "Invalid embedding request")
		return
	}
	response, code, err := h.modelProviderService.EmbedText(req.ProviderName, req.InstanceName, req.ModelName, user.ID, req.Texts, &models.APIConfig{Context: c.Request.Context(), ModelID: req.ModelID}, &models.EmbeddingConfig{Dimension: req.Dimension})
	if err != nil {
		jsonError(c, code, "Unable to embed texts")
		return
	}
	jsonResponse(c, common.CodeSuccess, response, "success")
}

type RerankDocumentRequest struct {
	ModelID      *int64   `json:"model_id"`
	ProviderName string   `json:"provider_name"`
	InstanceName string   `json:"instance_name"`
	ModelName    string   `json:"model_name"`
	Query        string   `json:"query" binding:"required"`
	Documents    []string `json:"documents" binding:"required,min=1"`
	TopN         int      `json:"top_n" binding:"gte=0"`
}

func (h *ProviderHandler) RerankDocument(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		c.JSON(http.StatusUnauthorized, gin.H{"code": code, "message": message})
		return
	}
	var req RerankDocumentRequest
	if err := c.ShouldBindJSON(&req); err != nil || validateModelSelection(req.ModelID, req.ProviderName, req.InstanceName, req.ModelName) != nil {
		jsonError(c, common.CodeBadRequest, "Invalid rerank request")
		return
	}
	response, code, err := h.modelProviderService.RerankDocument(req.ProviderName, req.InstanceName, req.ModelName, user.ID, req.Query, req.Documents, &models.APIConfig{Context: c.Request.Context(), ModelID: req.ModelID}, &models.RerankConfig{TopN: req.TopN})
	if err != nil {
		jsonError(c, code, "Unable to rerank documents")
		return
	}
	jsonResponse(c, common.CodeSuccess, response.Data, "success")
}
