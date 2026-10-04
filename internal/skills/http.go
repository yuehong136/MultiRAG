package skills

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v4"
	"multirag/internal/entity"
	"multirag/internal/utility"
)

// Authenticate implements Python's JWT subject and API-token tenant contract.
func (s *Service) Authenticate(ctx context.Context, authorization, secret string) (string, error) {
	parts := strings.Fields(authorization)
	if len(parts) == 0 {
		return "", fault(401, "UNAUTHORIZED")
	}
	token := parts[0]
	if len(parts) > 1 {
		token = parts[1]
	}
	if secret != "" {
		claims := jwt.MapClaims{}
		parsed, e := jwt.ParseWithClaims(token, claims, func(t *jwt.Token) (any, error) {
			if t.Method != jwt.SigningMethodHS256 {
				return nil, fault(401, "UNAUTHORIZED")
			}
			return []byte(secret), nil
		}, jwt.WithValidMethods([]string{"HS256"}))
		if e == nil && parsed.Valid {
			email, _ := claims["sub"].(string)
			if email != "" {
				var user entity.User
				if s.DB.WithContext(ctx).Where("email = ?", email).First(&user).Error == nil && (user.AccessToken == nil || !strings.HasPrefix(*user.AccessToken, "INVALID_")) {
					return user.ID, nil
				}
			}
		}
		access, e := utility.ExtractAccessToken(authorization, secret)
		if e == nil && len(access) >= 32 {
			var user entity.User
			if s.DB.WithContext(ctx).Where("access_token = ?", access).First(&user).Error == nil && !strings.HasPrefix(access, "INVALID_") {
				return user.ID, nil
			}
		}
	}
	if os.Getenv("DISABLE_SDK") != "" {
		return "", fault(401, "UNAUTHORIZED")
	}
	var apiToken entity.APIToken
	if s.DB.WithContext(ctx).Where("token = ?", token).First(&apiToken).Error == nil {
		return apiToken.TenantID, nil
	}
	return "", fault(401, "UNAUTHORIZED")
}
func respond(c *gin.Context, status int, data any, e error) {
	if e != nil {
		var problem *Error
		if !errors.As(e, &problem) {
			problem = &Error{503, "SERVICE_UNAVAILABLE", "Service unavailable"}
		}
		c.JSON(problem.Status, JSON{"code": problem.Status, "message": problem.Message, "data": JSON{"error_code": problem.Code}})
		return
	}
	c.JSON(status, JSON{"code": 0, "message": "success", "data": data})
}
func body(c *gin.Context) (JSON, error) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1024*1024)
	decoder := json.NewDecoder(c.Request.Body)
	var data JSON
	if e := decoder.Decode(&data); e != nil || data == nil {
		return nil, fault(422, "INVALID_REQUEST")
	}
	return data, nil
}
func pagination(c *gin.Context) (int, int, error) {
	page, size := 1, 20
	var e error
	if raw := c.Query("page"); raw != "" {
		page, e = strconv.Atoi(raw)
		if e != nil {
			return 0, 0, fault(422, "INVALID_PAGINATION")
		}
	}
	if raw := c.Query("page_size"); raw != "" {
		size, e = strconv.Atoi(raw)
		if e != nil {
			return 0, 0, fault(422, "INVALID_PAGINATION")
		}
	}
	if page < 1 || page > 100000000 || size < 1 || size > 100 {
		return 0, 0, fault(422, "INVALID_PAGINATION")
	}
	return page, size, nil
}
func (s *Service) Register(engine *gin.Engine, secret func() string) {
	group := engine.Group("/api/v1/skills")
	group.Use(func(c *gin.Context) {
		tenant, e := s.Authenticate(c.Request.Context(), c.GetHeader("Authorization"), secret())
		if e != nil {
			respond(c, 0, nil, e)
			c.Abort()
			return
		}
		if e = s.Ready(c.Request.Context()); e != nil {
			respond(c, 0, nil, e)
			c.Abort()
			return
		}
		c.Set("skills_tenant", tenant)
	})
	type endpoint func(*gin.Context, string) (any, error)
	add := func(method, path string, status int, fn endpoint) {
		group.Handle(method, path, func(c *gin.Context) {
			data, e := fn(c, c.GetString("skills_tenant"))
			if !c.Writer.Written() {
				respond(c, status, data, e)
			}
		})
	}
	add("GET", "/capabilities", 200, func(c *gin.Context, t string) (any, error) {
		return JSON{"backend": "go", "schema_version": 1, "sources": []string{"local"}, "search_modes": []string{"keyword", "vector", "hybrid"}, "search_available": s.Index != nil, "storage_available": s.Blobs != nil}, nil
	})
	add("GET", "/models", 200, func(c *gin.Context, t string) (any, error) {
		if s.Models == nil {
			return JSON{"models": []JSON{}}, nil
		}
		models, e := s.Models.List(c.Request.Context(), t)
		return JSON{"models": models}, e
	})
	add("GET", "/spaces", 200, func(c *gin.Context, t string) (any, error) {
		page, size, e := pagination(c)
		if e != nil {
			return nil, e
		}
		q := s.DB.WithContext(c.Request.Context()).Model(&Space{}).Where("tenant_id = ? AND state = 'active'", t)
		if k := c.Query("keywords"); k != "" {
			q = q.Where("name ILIKE ?", "%"+k+"%")
		}
		var total int64
		if e = q.Count(&total).Error; e != nil {
			return nil, e
		}
		items := []Space{}
		e = q.Order("create_time DESC,id").Offset((page - 1) * size).Limit(size).Find(&items).Error
		return JSON{"spaces": items, "total": total, "page": page, "page_size": size}, e
	})
	add("POST", "/spaces", 200, func(c *gin.Context, t string) (any, error) {
		b, e := body(c)
		if e != nil {
			return nil, e
		}
		return s.CreateSpace(c.Request.Context(), t, stringValue(b["name"]), stringValue(b["description"]))
	})
	add("GET", "/spaces/:space_id", 200, func(c *gin.Context, t string) (any, error) {
		return s.Space(c.Request.Context(), t, c.Param("space_id"))
	})
	add("PATCH", "/spaces/:space_id", 200, func(c *gin.Context, t string) (any, error) {
		b, e := body(c)
		if e != nil {
			return nil, e
		}
		revision, ok := number(b["revision"])
		if !ok || revision < 1 || revision != float64(int64(revision)) {
			return nil, fault(422, "INVALID_REVISION")
		}
		var name, description *string
		if v, ok := b["name"]; ok {
			str, ok := v.(string)
			if !ok {
				return nil, fault(422, "INVALID_NAME")
			}
			name = &str
		}
		if v, ok := b["description"]; ok {
			str, ok := v.(string)
			if !ok {
				return nil, fault(422, "INVALID_DESCRIPTION")
			}
			description = &str
		}
		return s.PatchSpace(c.Request.Context(), t, c.Param("space_id"), int64(revision), name, description)
	})
	for _, route := range []struct{ path, kind, param string }{{"/spaces/:space_id", "delete_space", "space_id"}, {"/spaces/:space_id/skills/:skill_id", "delete_skill", "skill_id"}, {"/spaces/:space_id/versions/:version_id", "delete_version", "version_id"}} {
		r := route
		add("DELETE", r.path, 202, func(c *gin.Context, t string) (any, error) {
			op, e := s.Enqueue(c.Request.Context(), t, c.Param("space_id"), c.Param(r.param), r.kind, c.GetHeader("Idempotency-Key"), JSON{})
			return op.Accepted(), e
		})
	}
	for _, route := range []struct{ path, kind string }{{"/spaces/delete", "delete_spaces"}, {"/spaces/:space_id/skills/delete", "delete_skills"}} {
		r := route
		add("POST", r.path, 202, func(c *gin.Context, t string) (any, error) {
			b, e := body(c)
			if e != nil {
				return nil, e
			}
			raw, ok := b["ids"].([]any)
			if !ok {
				return nil, fault(422, "INVALID_IDS")
			}
			ids := []string{}
			for _, v := range raw {
				id, ok := v.(string)
				if !ok || id == "" {
					return nil, fault(422, "INVALID_IDS")
				}
				ids = append(ids, id)
			}
			op, e := s.Batch(c.Request.Context(), t, c.Param("space_id"), r.kind, c.GetHeader("Idempotency-Key"), ids)
			return op.Accepted(), e
		})
	}
	add("GET", "/spaces/:space_id/skills", 200, func(c *gin.Context, t string) (any, error) {
		sp, e := s.Space(c.Request.Context(), t, c.Param("space_id"))
		if e != nil {
			return nil, e
		}
		page, size, e := pagination(c)
		if e != nil {
			return nil, e
		}
		q := s.DB.WithContext(c.Request.Context()).Model(&Skill{}).Where("tenant_id = ? AND space_id = ? AND state = 'active'", t, sp.ID)
		if k := c.Query("keywords"); k != "" {
			q = q.Where("name ILIKE ?", "%"+k+"%")
		}
		sort := c.DefaultQuery("sort", "name")
		if sort != "name" && sort != "create_time" {
			return nil, fault(422, "INVALID_SORT")
		}
		if c.Query("desc") == "true" {
			sort += " DESC"
		}
		var total int64
		if e = q.Count(&total).Error; e != nil {
			return nil, e
		}
		items := []Skill{}
		e = q.Order(sort + ",id").Offset((page - 1) * size).Limit(size).Find(&items).Error
		return JSON{"skills": items, "total": total, "page": page, "page_size": size}, e
	})
	add("GET", "/spaces/:space_id/skills/:skill_id", 200, func(c *gin.Context, t string) (any, error) {
		if _, e := s.Space(c.Request.Context(), t, c.Param("space_id")); e != nil {
			return nil, e
		}
		var skill Skill
		e := s.DB.WithContext(c.Request.Context()).Where("id = ? AND tenant_id = ? AND space_id = ? AND state = 'active'", c.Param("skill_id"), t, c.Param("space_id")).First(&skill).Error
		if e != nil {
			return nil, missing(e)
		}
		versions := []Version{}
		e = s.DB.WithContext(c.Request.Context()).Where("tenant_id = ? AND skill_id = ? AND state = 'installed'", t, skill.ID).Order("create_time DESC,id").Find(&versions).Error
		return JSON{"skill": skill, "versions": versions}, e
	})
	add("POST", "/spaces/:space_id/versions", 202, func(c *gin.Context, t string) (any, error) {
		if _, e := spaceIn(s.DB.WithContext(c.Request.Context()), t, c.Param("space_id"), true); e != nil {
			return nil, e
		}
		if e := validateKey(c.GetHeader("Idempotency-Key")); e != nil {
			return nil, e
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxPackage+2*1024*1024)
		reader, e := c.Request.MultipartReader()
		if e != nil {
			return nil, fault(422, "INVALID_MULTIPART")
		}
		pkg, e := parsePackage(reader)
		if e != nil {
			return nil, e
		}
		op, e := s.Install(c.Request.Context(), t, c.Param("space_id"), c.GetHeader("Idempotency-Key"), pkg)
		return op.Accepted(), e
	})
	add("PUT", "/spaces/:space_id/skills/:skill_id/active-version", 202, func(c *gin.Context, t string) (any, error) {
		b, e := body(c)
		if e != nil {
			return nil, e
		}
		if _, ok := b["version_id"]; !ok {
			return nil, fault(422, "VERSION_REQUIRED")
		}
		if b["version_id"] != nil {
			if v, ok := b["version_id"].(string); !ok || v == "" {
				return nil, fault(422, "INVALID_VERSION")
			}
		}
		op, e := s.Enqueue(c.Request.Context(), t, c.Param("space_id"), c.Param("skill_id"), "activate", c.GetHeader("Idempotency-Key"), b)
		return op.Accepted(), e
	})
	add("GET", "/spaces/:space_id/config", 200, func(c *gin.Context, t string) (any, error) {
		return s.Config(c.Request.Context(), t, c.Param("space_id"))
	})
	add("PATCH", "/spaces/:space_id/config", 200, func(c *gin.Context, t string) (any, error) {
		b, e := body(c)
		if e != nil {
			return nil, e
		}
		cfg, e := s.PatchConfig(c.Request.Context(), t, c.Param("space_id"), b)
		return JSON{"config": cfg, "requires_reindex": true}, e
	})
	add("POST", "/spaces/:space_id/reindex", 202, func(c *gin.Context, t string) (any, error) {
		op, e := s.Enqueue(c.Request.Context(), t, c.Param("space_id"), c.Param("space_id"), "reindex", c.GetHeader("Idempotency-Key"), JSON{})
		return op.Accepted(), e
	})
	add("POST", "/spaces/:space_id/search", 200, func(c *gin.Context, t string) (any, error) {
		b, e := body(c)
		if e != nil {
			return nil, e
		}
		page, size := 1, 20
		if n, ok := number(b["page"]); ok {
			if n != float64(int(n)) {
				return nil, fault(422, "INVALID_PAGINATION")
			}
			page = int(n)
		}
		if n, ok := number(b["page_size"]); ok {
			if n != float64(int(n)) {
				return nil, fault(422, "INVALID_PAGINATION")
			}
			size = int(n)
		}
		mode := stringValue(b["mode"])
		if mode == "" {
			mode = "hybrid"
		}
		return s.Search(c.Request.Context(), t, c.Param("space_id"), stringValue(b["query"]), mode, page, size)
	})
	add("GET", "/operations/:operation_id", 200, func(c *gin.Context, t string) (any, error) {
		return s.Operation(c.Request.Context(), t, c.Param("operation_id"))
	})
	add("POST", "/operations/:operation_id/retry", 202, func(c *gin.Context, t string) (any, error) {
		op, e := s.Retry(c.Request.Context(), t, c.Param("operation_id"))
		return op.Accepted(), e
	})
	add("GET", "/spaces/:space_id/versions/:version_id/files", 200, func(c *gin.Context, t string) (any, error) {
		files, e := s.Files(c.Request.Context(), t, c.Param("space_id"), c.Param("version_id"))
		return JSON{"files": files}, e
	})
	add("GET", "/spaces/:space_id/versions/:version_id/file", 200, func(c *gin.Context, t string) (any, error) {
		files, e := s.Files(c.Request.Context(), t, c.Param("space_id"), c.Param("version_id"))
		if e != nil {
			return nil, e
		}
		for _, b := range files {
			if b.RelativePath == c.Query("path") {
				data, e := s.readFile(c.Request.Context(), t, b)
				if e != nil {
					return nil, e
				}
				c.Header("X-Content-Type-Options", "nosniff")
				c.Header("Content-Disposition", "attachment")
				c.Data(200, "application/octet-stream", data)
				return nil, nil
			}
		}
		return nil, fault(404, "NOT_FOUND")
	})
	add("GET", "/spaces/:space_id/versions/:version_id/download", 200, func(c *gin.Context, t string) (any, error) {
		files, e := s.Files(c.Request.Context(), t, c.Param("space_id"), c.Param("version_id"))
		if e != nil {
			return nil, e
		}
		var buffer bytes.Buffer
		archive := zip.NewWriter(&buffer)
		for _, b := range files {
			data, e := s.readFile(c.Request.Context(), t, b)
			if e != nil {
				return nil, e
			}
			writer, e := archive.Create(b.RelativePath)
			if e != nil {
				return nil, e
			}
			if _, e = writer.Write(data); e != nil {
				return nil, e
			}
		}
		if e = archive.Close(); e != nil {
			return nil, e
		}
		c.Header("X-Content-Type-Options", "nosniff")
		c.Header("Content-Disposition", "attachment; filename=skill.zip")
		c.Data(200, "application/zip", buffer.Bytes())
		return nil, nil
	})
}
