package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
	"net/url"
	"strconv"
	"strings"
)

// ImportMCPServers imports JSON configuration with per-entry atomic results.
func (h *MCPHandler) ImportMCPServers(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.ImportMCPRequest
	if bindObjectJSON(c, &req) != nil || req.Servers == nil {
		jsonError(c, common.CodeArgumentError, "Invalid MCP import payload.")
		return
	}
	results, code, err := h.mcpService.ImportMCPServers(c.Request.Context(), user.ID, req)
	if err != nil {
		// Earlier successful entries have committed under the per-entry import
		// contract; retain their receipts even when later work is cancelled.
		if len(results) > 0 {
			jsonResponse(c, code, gin.H{"results": results}, err.Error())
		} else {
			jsonError(c, code, err.Error())
		}
		return
	}
	jsonResponse(c, code, gin.H{"results": results}, "success")
}

// TestMCPServer uses unsaved body settings and never stores credentials.
func (h *MCPHandler) TestMCPServer(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.MCPDiscoveryRequest
	if bindObjectJSON(c, &req) != nil {
		jsonError(c, common.CodeArgumentError, "Invalid MCP discovery payload.")
		return
	}
	tools, code, err := h.mcpService.TestMCPServer(c.Request.Context(), user.ID, req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, tools, "success")
}

// GetMCPServerTools discovers tools only from an authorized saved configuration.
func (h *MCPHandler) GetMCPServerTools(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	query, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid MCP query.")
		return
	}
	seconds := float64(0)
	if raw, ok := query["timeout"]; ok {
		if len(raw) != 1 || raw[0] == "" || strings.TrimSpace(raw[0]) != raw[0] {
			jsonError(c, common.CodeArgumentError, "Invalid MCP timeout.")
			return
		}
		seconds, err = strconv.ParseFloat(raw[0], 64)
	}
	if err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid MCP query.")
		return
	}
	tools, code, err := h.mcpService.GetMCPServerTools(c.Request.Context(), user.ID, c.Param("mcp_id"), seconds)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, tools, "success")
}
