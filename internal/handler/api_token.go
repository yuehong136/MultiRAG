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
	"net/http"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/service"
)

// ListTokens list all API tokens for the current user's tenant
// @Summary List API Tokens
// @Description List all API tokens for the current user's tenant
// @Tags system
// @Accept json
// @Produce json
// @Security ApiKeyAuth
// @Success 200 {object} map[string]interface{}
// @Router /v1/system/token_list [get]
func (h *SystemHandler) ListTokens(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}

	// Get user's tenant with owner role
	userTenantDAO := dao.NewUserTenantDAO()
	tenants, err := userTenantDAO.GetByUserIDAndRole(user.ID, "owner")
	if err != nil || len(tenants) == 0 {
		jsonError(c, common.CodeDataError, "Tenant not found")
		return
	}
	tenantID := tenants[0].TenantID

	tokens, err := h.systemService.ListAPITokens(tenantID)
	if err != nil {
		jsonError(c, common.CodeServerError, "Failed to list tokens")
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    common.CodeSuccess,
		"message": "success",
		"data":    tokens,
	})
}

// CreateToken creates a new API token for the current user's tenant
// @Summary Create API Token
// @Description Generate a new API token for the current user's tenant
// @Tags system
// @Accept json
// @Produce json
// @Security ApiKeyAuth
// @Param name query string false "Name of the token"
// @Success 200 {object} map[string]interface{}
// @Router /v1/system/new_token [post]
func (h *SystemHandler) CreateToken(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}

	// Get user's tenant with owner role
	userTenantDAO := dao.NewUserTenantDAO()
	tenants, err := userTenantDAO.GetByUserIDAndRole(user.ID, "owner")
	if err != nil || len(tenants) == 0 {
		jsonError(c, common.CodeDataError, "Tenant not found")
		return
	}
	tenantID := tenants[0].TenantID

	// Parse request (name is optional, kept for API compatibility)
	var req service.CreateAPITokenRequest
	_ = c.ShouldBind(&req)

	token, err := h.systemService.CreateAPIToken(tenantID, &req)
	if err != nil {
		jsonError(c, common.CodeServerError, "Failed to create token")
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    common.CodeSuccess,
		"message": "success",
		"data":    token,
	})
}

// DeleteToken deletes an API token
// @Summary Delete API Token
// @Description Remove an API token for the current user's tenant
// @Tags system
// @Accept json
// @Produce json
// @Security ApiKeyAuth
// @Param token path string true "The API token to remove"
// @Success 200 {object} map[string]interface{}
// @Router /v1/system/token/{token} [delete]
func (h *SystemHandler) DeleteToken(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}

	// Get user's tenant with owner role
	userTenantDAO := dao.NewUserTenantDAO()
	tenants, err := userTenantDAO.GetByUserIDAndRole(user.ID, "owner")
	if err != nil || len(tenants) == 0 {
		jsonError(c, common.CodeDataError, "Tenant not found")
		return
	}
	tenantID := tenants[0].TenantID

	// Get token from path parameter
	token := c.Param("token")
	if token == "" {
		jsonError(c, common.CodeArgumentError, "Token is required")
		return
	}

	if err := h.systemService.DeleteAPIToken(tenantID, token); err != nil {
		jsonError(c, common.CodeServerError, "Failed to delete token")
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    common.CodeSuccess,
		"message": "success",
		"data":    true,
	})
}
