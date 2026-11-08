package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/service"
)

type FileCommitHandler struct{ service *service.FileCommitService }

func NewFileCommitHandler(s *service.FileCommitService) *FileCommitHandler {
	return &FileCommitHandler{service: s}
}

// RegisterFileCommitRoutes extends the existing authenticated v1 group. Existing
// Files and Datasets wildcard names are reused to avoid Gin radix-tree conflicts.
func RegisterFileCommitRoutes(group *gin.RouterGroup, h *FileCommitHandler) {
	for _, scope := range []struct{ prefix, param string }{{"folders", "folder_id"}, {"workspaces", "workspace_id"}, {"workspace", "workspace_id"}, {"datasets", "dataset_id"}} {
		for _, route := range []struct{ method, suffix, action string }{{"POST", "/commits", "create"}, {"GET", "/commits", "history"}, {"GET", "/commits/diff", "diff"}, {"GET", "/changes", "changes"}, {"GET", "/commits/:commit_id", "detail"}, {"GET", "/commits/:commit_id/files", "files"}, {"GET", "/commits/:commit_id/tree", "tree"}, {"GET", "/commits/:commit_id/files/:file_id/content", "content"}} {
			prefix, param, action := scope.prefix, scope.param, route.action
			group.Handle(route.method, "/"+prefix+"/:"+param+route.suffix, func(c *gin.Context) { h.serve(c, prefix, c.Param(param), action) })
		}
	}
	group.GET("/files/:id/versions", func(c *gin.Context) { h.serve(c, "files", c.Param("id"), "versions") })
}
func commitHTTPError(c *gin.Context, err error) {
	var failure *service.FileCommitError
	if errors.As(err, &failure) {
		c.JSON(failure.Status, gin.H{"code": failure.Status, "message": failure.Message, "data": nil})
		return
	}
	c.JSON(http.StatusServiceUnavailable, gin.H{"code": 503, "message": "File snapshot operation failed", "data": nil})
}
func parseCommitRequest(c *gin.Context) (entity.CreateFileCommit, error) {
	var req entity.CreateFileCommit
	body, err := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, 64*1024*1024))
	if err != nil {
		return req, err
	}
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		return req, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return req, errors.New("invalid JSON body")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return req, err
	}
	if fields["message"] == nil || string(fields["message"]) == "null" || fields["files"] == nil || string(fields["files"]) == "null" {
		return req, errors.New("message and files required")
	}
	_, req.ParentSpecified = fields["parent_id"]
	return req, nil
}
func (h *FileCommitHandler) serve(c *gin.Context, prefix, id, action string) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	ctx := c.Request.Context()
	svc := h.service
	var data any
	var err error
	if action == "create" {
		var req entity.CreateFileCommit
		req, err = parseCommitRequest(c)
		if err != nil {
			c.JSON(422, gin.H{"code": 422, "message": "Invalid commit request", "data": nil})
			return
		}
		data, err = svc.Create(ctx, prefix, id, user.ID, req)
	} else if action == "versions" {
		data, err = svc.Versions(ctx, user.ID, id)
	} else {
		db := svc.DB
		if db == nil {
			db = dao.DB
		}
		var folder *entity.File
		folder, err = svc.Resolve(ctx, db, prefix, id, user.ID, false, false)
		if err != nil {
			commitHTTPError(c, err)
			return
		}
		d := dao.FileCommitDAO{DB: db, TenantID: folder.TenantID, FolderID: folder.ID}
		switch action {
		case "history":
			page, size := 1, 15
			if raw, ok := c.GetQuery("page"); ok {
				page, err = strconv.Atoi(raw)
			}
			if err == nil {
				if raw, ok := c.GetQuery("page_size"); ok {
					size, err = strconv.Atoi(raw)
				}
			}
			desc := true
			if raw, ok := c.GetQuery("desc"); ok {
				desc, err = strconv.ParseBool(raw)
			}
			if err != nil || page < 1 || page > 10000000 || size < 1 || size > 100 {
				c.JSON(422, gin.H{"code": 422, "message": "Invalid pagination", "data": nil})
				return
			}
			var commits []entity.FileCommit
			var total int64
			commits, total, err = d.List(ctx, page, size, desc)
			for i := range commits {
				commits[i].TreeState = nil
			}
			data = gin.H{"total": total, "page": page, "page_size": size, "commits": commits}
		case "changes":
			data, err = svc.Changes(ctx, folder)
		case "diff":
			var before, after *entity.FileCommit
			before, err = svc.Get(ctx, folder, c.Query("from"))
			if err == nil {
				after, err = svc.Get(ctx, folder, c.Query("to"))
			}
			if err == nil {
				data = service.DiffSnapshotStates(before.TreeState, after.TreeState)
			}
		default:
			var commit *entity.FileCommit
			commit, err = svc.Get(ctx, folder, c.Param("commit_id"))
			if err == nil {
				switch action {
				case "tree":
					data, err = service.SnapshotTree(commit.TreeState, folder.ID)
				case "content":
					var b []byte
					b, err = svc.Content(ctx, commit, c.Param("file_id"))
					data = gin.H{"content": strings.ToValidUTF8(string(b), "�")}
				case "files":
					data, err = d.Items(ctx, commit.ID)
				case "detail":
					var items []entity.FileCommitItem
					items, err = d.Items(ctx, commit.ID)
					commit.TreeState = nil
					data = gin.H{"id": commit.ID, "folder_id": commit.FolderID, "parent_id": commit.ParentID, "sequence": commit.Sequence, "message": commit.Message, "author_id": commit.AuthorID, "file_count": commit.FileCount, "create_time": commit.CreateTime, "files": items}
				}
			}
		}
	}
	if err != nil {
		commitHTTPError(c, err)
		return
	}
	jsonResponse(c, common.CodeSuccess, data, "success")
}
