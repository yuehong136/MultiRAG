package router

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/handler"
)

// RegisterAgentExecutionRoutes adds the supported Go canvas operations to v1.
// Existing discovery, version/session reads and uploads retain their handlers.
func RegisterAgentExecutionRoutes(v1 *gin.RouterGroup, h *handler.AgentHandler) {
	v1.POST("/agents", h.CreateAgent)
	v1.GET("/agents/:agent_id", h.GetAgent)
	v1.PUT("/agents/:agent_id", h.UpdateAgent)
	v1.DELETE("/agents/:agent_id", h.DeleteAgent)
	v1.POST("/agents/:agent_id/publish", h.PublishAgent)
	v1.DELETE("/agents/:agent_id/versions/:version_id", h.DeleteAgentVersion)
	v1.POST("/agents/:agent_id/run", h.RunAgent)
	v1.DELETE("/agents/:agent_id/run", h.CancelAgent)
}
