package handler

import (
	"context"
	"errors"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
	"multirag/internal/utility"
)

type documentFileReader interface {
	GetDocumentPreview(context.Context, string, string, bool) (*service.DocumentFile, error)
	DownloadDocument(context.Context, string, string, string) (*service.DocumentFile, error)
	GetDocumentArtifact(context.Context, string, string, string, *string) (*service.DocumentFile, error)
}

func documentFileHeaders(c *gin.Context) {
	c.Header("Cache-Control", "private, no-store")
	c.Header("X-Content-Type-Options", "nosniff")
}
func documentFileError(c *gin.Context, err error) {
	status, code, message := http.StatusInternalServerError, common.CodeExceptionError, "Document could not be read."
	if errors.Is(err, service.ErrDocumentFileUnavailable) {
		status, code, message = http.StatusNotFound, common.CodeDataError, "Document not found!"
	}
	if errors.Is(err, service.ErrDocumentDownloadDenied) {
		status, code, message = http.StatusForbidden, common.CodeForbidden, "No download permission. Please contact your administrator."
	}
	c.JSON(status, gin.H{"code": code, "message": message, "data": nil})
}
func sendDocumentFile(c *gin.Context, file *service.DocumentFile, download bool) {
	if file == nil {
		documentFileError(c, service.ErrDocumentFileStorage)
		return
	}
	disposition := "inline"
	mediaType, _, err := mime.ParseMediaType(file.ContentType)
	if err != nil {
		mediaType = "application/octet-stream"
	}
	if download || utility.ShouldForceAttachment(utility.GetFileExtension(file.FileName), mediaType) {
		disposition = "attachment"
	}
	// QueryEscape is converted to RFC 5987 percent encoding (spaces are not '+').
	encoded := strings.ReplaceAll(url.QueryEscape(file.FileName), "+", "%20")
	fallback := strings.Map(func(r rune) rune {
		if r < 32 || r >= 127 || strings.ContainsRune("\"\\/", r) {
			return '_'
		}
		return r
	}, file.FileName)
	c.Header("Content-Disposition", disposition+"; filename="+strconv.Quote(fallback)+"; filename*=UTF-8''"+encoded)
	c.Data(http.StatusOK, file.ContentType, file.Data)
}
func (h *DocumentHandler) binaryReader() documentFileReader {
	if h.fileService != nil {
		return h.fileService
	}
	return service.NewDocumentFileService()
}
func (h *DocumentHandler) GetDocumentPreview(c *gin.Context)   { h.readDocumentFile(c, false) }
func (h *DocumentHandler) DownloadDocumentFile(c *gin.Context) { h.readDocumentFile(c, true) }
func (h *DocumentHandler) readDocumentFile(c *gin.Context, download bool) {
	documentFileHeaders(c)
	user, code, _ := GetUser(c)
	if code != common.CodeSuccess || user == nil || user.ID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"code": common.CodeAuthenticationError, "message": "Unauthorized", "data": nil})
		return
	}
	file, err := h.binaryReader().GetDocumentPreview(c.Request.Context(), user.ID, c.Param("id"), download)
	if err != nil {
		documentFileError(c, err)
		return
	}
	sendDocumentFile(c, file, download)
}

// DownloadDocument requires DatasetDocumentAuthMiddleware's API-token tenant.
func (h *DocumentHandler) DownloadDocument(c *gin.Context) {
	documentFileHeaders(c)
	tenantID := c.GetString("document_token_tenant")
	if tenantID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"code": common.CodeAuthenticationError, "message": "Unauthorized", "data": nil})
		return
	}
	file, err := h.binaryReader().DownloadDocument(c.Request.Context(), tenantID, c.Param("dataset_id"), c.Param("document_id"))
	if err != nil {
		documentFileError(c, err)
		return
	}
	sendDocumentFile(c, file, true)
}
func (h *DocumentHandler) GetDocumentArtifact(c *gin.Context) {
	documentFileHeaders(c)
	user, code, _ := GetUser(c)
	if code != common.CodeSuccess || user == nil || user.ID == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"code": common.CodeAuthenticationError, "message": "Unauthorized", "data": nil})
		return
	}
	values, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil || len(values["run_id"]) != 1 || len(values["session_id"]) > 1 {
		documentFileError(c, service.ErrDocumentFileUnavailable)
		return
	}
	var sessionID *string
	if values.Has("session_id") {
		v := values.Get("session_id")
		sessionID = &v
	}
	file, err := h.binaryReader().GetDocumentArtifact(c.Request.Context(), user.ID, c.Param("filename"), values.Get("run_id"), sessionID)
	if err != nil {
		documentFileError(c, err)
		return
	}
	sendDocumentFile(c, file, false)
}
