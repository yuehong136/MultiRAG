package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	mc "github.com/milvus-io/milvus/client/v2/milvusclient"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
)

// AttachSkillCore registers the explicit namespace and chosen legacy alias.
// Core tables, objects and index namespaces belong to this Go deployment.
func AttachSkillCore(r *gin.Engine, cfg *server.Config, authenticate func(context.Context, string, string) (string, error), listModels func(context.Context, string) (any, error), blobs service.SkillBlobStore) (func(), error) {
	protocol, e := server.SkillsProtocol()
	if e != nil {
		return nil, e
	}

	unavailable := func(reason error) (func(), error) {
		if protocol == server.SkillsCoreProtocol {
			return nil, reason
		}
		r.Any("/api/v1/skill-core/*path", func(c *gin.Context) {
			c.AbortWithStatusJSON(503, gin.H{"code": 503, "message": "Skills core unavailable for this deployment", "data": gin.H{"error_code": "SKILL_CORE_UNAVAILABLE"}})
		})
		registerSkillProtocols(r, protocol, false)
		return func() {}, nil
	}
	if cfg.DocEngine.Type != server.EngineMilvus || cfg.DocEngine.Milvus == nil {
		return unavailable(fmt.Errorf("Go Skills core requires the verified Milvus adapter"))
	}
	if blobs == nil {
		return unavailable(fmt.Errorf("Go Skills core requires strict MinIO storage"))
	}
	if e = dao.MigrateSkillCore(dao.DB); e != nil {
		return unavailable(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	m := cfg.DocEngine.Milvus
	connect, done := context.WithTimeout(ctx, 10*time.Second)
	doc, e := engine.NewSkillMilvus(connect, &mc.ClientConfig{Address: m.Hosts, Username: m.Username, Password: m.Password, APIKey: m.Token, DBName: m.DBName})
	done()
	if e != nil {
		cancel()
		return unavailable(e)
	}
	service.SetSkillCoreResources(doc, blobs)
	h := NewSkillSearchHandler(doc)
	register := func(base string) {
		group := r.Group(base)
		group.Use(func(c *gin.Context) {
			secret := ""
			if vars := server.GetVariables(); vars != nil {
				secret = vars.SecretKey
			}
			tenant, e := authenticate(c.Request.Context(), c.GetHeader("Authorization"), secret)
			if e != nil {
				c.AbortWithStatusJSON(401, gin.H{"code": 401, "message": "Unauthorized", "data": nil})
				return
			}
			c.Set("user", &entity.User{ID: tenant})
		})
		group.Use(h.coreBoundary)
		group.GET("/spaces", h.ListSpaces)
		group.POST("/spaces", h.CreateSpace)
		group.GET("/spaces/:space_id", h.GetSpace)
		group.PUT("/spaces/:space_id", h.UpdateSpace)
		group.DELETE("/spaces/:space_id", h.DeleteSpace)
		group.GET("/space/by-folder", h.GetSpaceByFolder)
		group.GET("/config", h.GetConfig)
		group.POST("/config", h.UpdateConfig)
		group.POST("/search", h.Search)
		group.POST("/index", h.IndexSkills)
		group.DELETE("/index", h.DeleteSkillIndex)
		group.POST("/reindex", h.Reindex)
		group.GET("/models", func(c *gin.Context) {
			user, _, _ := GetUser(c)
			rows, e := listModels(c.Request.Context(), user.ID)
			if e != nil {
				jsonError(c, common.CodeOperatingError, "Models unavailable")
				return
			}
			jsonResponse(c, common.CodeSuccess, gin.H{"models": rows}, "success")
		})
	}
	register("/api/v1/skill-core")
	if protocol == server.SkillsCoreProtocol {
		register("/api/v1/skills")
	}
	registerSkillProtocols(r, protocol, true)

	go h.spaceService.RecoverDeletes(ctx, doc)
	return func() { cancel(); _ = doc.Client.Close(context.Background()) }, nil
}

// coreBoundary validates ownership and serializes index/config/delete mutations
// across processes. The lock is held on one SQL connection, not a pooled session.
func (h *SkillSearchHandler) coreBoundary(c *gin.Context) {
	user, _, _ := GetUser(c)
	space := c.Param("space_id")
	if space == "" {
		space = c.Query("space_id")
	}
	if c.Request.Method == "POST" && c.Request.Body != nil {
		raw, e := io.ReadAll(io.LimitReader(c.Request.Body, 32<<20))
		if e != nil {
			c.AbortWithStatus(400)
			return
		}
		c.Request.Body = io.NopCloser(bytes.NewReader(raw))
		var body map[string]json.RawMessage
		if json.Unmarshal(raw, &body) == nil {
			if v := body["space_id"]; v != nil {
				_ = json.Unmarshal(v, &space)
			}
		}
	}
	space = strings.TrimSpace(space)
	required := false
	for _, suffix := range []string{"/config", "/search", "/index", "/reindex"} {
		if strings.HasSuffix(c.Request.URL.Path, suffix) {
			required = true
		}
	}
	if required && space == "" {
		c.AbortWithStatusJSON(400, gin.H{"code": 400, "message": "space_id is required", "data": gin.H{"error_code": "SPACE_REQUIRED"}})
		return
	}
	if space == "" {
		c.Next()
		return
	}
	db, e := dao.DB.DB()
	if e != nil {
		c.AbortWithStatus(503)
		return
	}
	conn, e := db.Conn(c.Request.Context())
	if e != nil {
		c.AbortWithStatus(503)
		return
	}
	defer conn.Close()
	key := "go-skills:" + user.ID + ":" + space
	if _, e = conn.ExecContext(c.Request.Context(), "SELECT pg_advisory_lock(hashtextextended($1,0))", key); e != nil {
		c.AbortWithStatus(503)
		return
	}
	defer conn.ExecContext(context.Background(), "SELECT pg_advisory_unlock(hashtextextended($1,0))", key)
	row, e := dao.NewSkillSpaceDAO().GetByIDAnyStatus(space)
	allowDeleting := c.Request.Method == "GET" && c.Param("space_id") != "" || c.Request.Method == "DELETE" && c.Param("space_id") != ""
	if e != nil || row.TenantID != user.ID || row.Status == entity.SpaceStatusDeleted || (!allowDeleting && dao.CoreSpaceUnavailable(row)) {
		c.AbortWithStatusJSON(404, gin.H{"code": 404, "message": "space not found", "data": nil})
		return
	}
	c.Next()
}

func registerSkillProtocols(r *gin.Engine, protocol string, available bool) {
	r.GET("/api/v1/skill-protocols", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"code": 0, "message": "success", "data": gin.H{"default_protocol": protocol, "backend": "go", "protocols": []gin.H{{"protocol": server.SkillsCoreProtocol, "base_path": "/api/v1/skill-core", "writable": available, "capabilities": gin.H{"available": available, "rerank": false, "operations": false, "writable": available}}, {"protocol": server.SkillsAssetsProtocol, "base_path": "/api/v1/skill-assets", "writable": protocol == server.SkillsAssetsProtocol, "capabilities": gin.H{"rerank": true, "operations": true, "writable": protocol == server.SkillsAssetsProtocol}}}}})
	})
}
