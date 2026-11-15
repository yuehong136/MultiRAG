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
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"

	"multirag/internal/agent/runtime"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

// AdminRuntimeHandler exposes the per-tenant canvas-runtime override API
// used by the Phase 6 canary operators. It is intentionally small — the
// selector is the only collaborator it needs.
type AdminRuntimeHandler struct {
	selector *runtime.Selector
}

// NewAdminRuntimeHandler constructs an AdminRuntimeHandler backed by the
// supplied Selector. A nil selector is treated as a misconfiguration and
// the handler refuses every request with HTTP 500.
func NewAdminRuntimeHandler(selector *runtime.Selector) *AdminRuntimeHandler {
	return &AdminRuntimeHandler{selector: selector}
}

// setRuntimeRequest is the wire shape for POST
// /api/v1/admin/canvas-runtime/:tenant_id. The mode is required; empty or
// unknown values yield 400.
type setRuntimeRequest struct {
	Runtime string `json:"runtime"`
}

// setRuntimeResponse is what the operator sees in the 200 body.
type setRuntimeResponse struct {
	Code     common.ErrorCode `json:"code"`
	TenantID string           `json:"tenant_id"`
	Runtime  string           `json:"runtime"`
	Message  string           `json:"message"`
}

// ErrSelectorNotConfigured is returned when the handler was constructed
// without a backing Selector. It maps to HTTP 500 in the response path.
var ErrSelectorNotConfigured = errors.New("admin runtime: selector not configured")

// SetTenantRuntime implements POST /api/v1/admin/canvas-runtime/:tenant_id.
//
// Only an active owner or administrator of the target tenant may change its
// override. Authorization is checked before writing to the selector.
func (h *AdminRuntimeHandler) SetTenantRuntime(c *gin.Context) {
	user, code, _ := GetUser(c)
	if code != common.CodeSuccess || user == nil || user.ID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"code": common.CodeUnauthorized, "message": "Authentication required"})
		return
	}

	tenantID := c.Param("tenant_id")
	if tenantID == "" {
		jsonError(c, common.CodeArgumentError, "tenant_id is required")
		return
	}
	if dao.DB == nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": common.CodeServerError, "message": "Runtime authorization unavailable"})
		return
	}
	var memberships int64
	err := dao.DB.WithContext(c.Request.Context()).Model(&entity.UserTenant{}).
		Joins("JOIN user ON user.id = user_tenant.user_id AND user.status = ? AND user.is_active = ?", "1", "1").
		Joins("JOIN tenant ON tenant.id = user_tenant.tenant_id AND tenant.status = ?", "1").
		Where("user_tenant.user_id = ? AND user_tenant.tenant_id = ? AND user_tenant.status = ? AND user_tenant.role IN ?", user.ID, tenantID, "1", []string{"owner", "admin"}).Count(&memberships).Error
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": common.CodeServerError, "message": "Runtime authorization unavailable"})
		return
	}
	if memberships != 1 {
		c.JSON(http.StatusForbidden, gin.H{"code": common.CodeForbidden, "message": "Tenant administrator required"})
		return
	}
	if h.selector == nil {
		jsonError(c, common.CodeExceptionError, ErrSelectorNotConfigured.Error())
		return
	}

	var req setRuntimeRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid request body: "+err.Error())
		return
	}

	mode := runtime.RuntimeMode(req.Runtime)
	switch mode {
	case runtime.RuntimeGo, runtime.RuntimePython, runtime.RuntimeAuto:
		// allowed
	default:
		jsonError(c, common.CodeArgumentError,
			"runtime must be one of: go, python, auto")
		return
	}

	if err := h.selector.Set(c.Request.Context(), tenantID, mode); err != nil {
		jsonError(c, common.CodeDataError, err.Error())
		return
	}

	c.JSON(http.StatusOK, setRuntimeResponse{
		Code:     common.CodeSuccess,
		TenantID: tenantID,
		Runtime:  string(mode),
		Message:  "ok",
	})
}
