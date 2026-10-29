package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"unicode"

	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	"multirag/internal/cache"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"multirag/internal/utility"
)

var ErrDocumentFileUnavailable = errors.New("document not found")
var ErrDocumentDownloadDenied = errors.New("no download permission")
var ErrDocumentFileStorage = errors.New("document could not be read")

// DocumentFile contains authorized source bytes; empty objects remain valid files.
type DocumentFile struct {
	Data        []byte
	FileName    string
	ContentType string
}

type artifactBinding struct {
	OwnerID   string  `json:"owner_id"`
	RunID     string  `json:"run_id"`
	SessionID *string `json:"session_id"`
}

// DocumentFileService keeps binary access separate from document management.
type DocumentFileService struct {
	db            *gorm.DB
	store         storage.Storage
	bindingReader func(context.Context, string) (string, error)
}

func NewDocumentFileService() *DocumentFileService { return &DocumentFileService{db: dao.DB} }

func (s *DocumentFileService) readObject(ctx context.Context, bucket, key string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	store := s.store
	if store == nil {
		store = storage.GetStorageFactory().GetStorage()
	}
	if store == nil {
		return nil, ErrDocumentFileStorage
	}
	confirmed := false
	if probe, ok := store.(interface {
		ObjectExistsContext(context.Context, string, string) (bool, error)
	}); ok {
		exists, err := probe.ObjectExistsContext(ctx, bucket, key)
		if err != nil {
			return nil, ErrDocumentFileStorage
		}
		if !exists {
			return nil, ErrDocumentFileUnavailable
		}
		confirmed = true
	}
	data, err := store.Get(bucket, key)
	if err != nil {
		if errors.Is(err, storage.ErrNotFound) || errors.Is(err, storage.ErrBucketNotFound) {
			return nil, ErrDocumentFileUnavailable
		}
		return nil, ErrDocumentFileStorage
	}
	if data == nil {
		if !confirmed {
			return nil, ErrDocumentFileUnavailable
		}
		data = []byte{}
	}
	if err = ctx.Err(); err != nil {
		return nil, err
	}
	return data, nil
}

// GetDocumentPreview resolves fresh membership and exact registered source bytes.
func (s *DocumentFileService) GetDocumentPreview(ctx context.Context, userID, docID string, download bool) (*DocumentFile, error) {
	if s.db == nil {
		return nil, ErrDocumentFileStorage
	}
	var row struct {
		entity.Document
		TenantID string
		Role     string
	}
	err := s.db.WithContext(ctx).Table("t_ai_documents AS doc").
		Select("doc.*,kb.tenant_id,membership.role").
		Joins("JOIN t_ai_knowledgebases AS kb ON kb.id=doc.kb_id AND kb.status='1'").
		Joins("JOIN t_ai_tenants AS tenant ON tenant.id=kb.tenant_id AND tenant.status='1'").
		Joins("JOIN t_ai_user_tenants AS membership ON membership.tenant_id=kb.tenant_id AND membership.user_id=? AND membership.status='1' AND membership.role IN ('owner','admin','normal')", userID).
		Joins("JOIN t_ai_users AS actor ON actor.id=membership.user_id AND actor.status='1' AND actor.is_active=true").
		Where("doc.id=? AND doc.status='1'", docID).Take(&row).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrDocumentFileUnavailable
	}
	if err != nil {
		return nil, err
	}
	if download && row.Role != "owner" && row.Role != "admin" {
		return nil, ErrDocumentDownloadDenied
	}
	return s.readDocument(ctx, &row.Document)
}

// DownloadDocument uses the API token's tenant, never a request-supplied owner.
func (s *DocumentFileService) DownloadDocument(ctx context.Context, tenantID, datasetID, docID string) (*DocumentFile, error) {
	if s.db == nil {
		return nil, ErrDocumentFileStorage
	}
	var doc entity.Document
	err := s.db.WithContext(ctx).Model(&entity.Document{}).
		Joins("JOIN t_ai_knowledgebases AS kb ON kb.id=t_ai_documents.kb_id AND kb.status='1' AND kb.tenant_id=?", tenantID).
		Joins("JOIN t_ai_tenants AS tenant ON tenant.id=kb.tenant_id AND tenant.status='1'").
		Where("t_ai_documents.id=? AND t_ai_documents.kb_id=? AND t_ai_documents.status='1'", docID, datasetID).Take(&doc).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrDocumentFileUnavailable
	}
	if err != nil {
		return nil, err
	}
	return s.readDocument(ctx, &doc)
}

func safeStorageAddress(bucket, key string) bool {
	if bucket == "" || bucket == "." || bucket == ".." || strings.ContainsAny(bucket, "/\\") || strings.ContainsFunc(bucket, unicode.IsControl) {
		return false
	}
	if key == "" || strings.HasPrefix(key, "/") || strings.Contains(key, "\\") || strings.ContainsFunc(key, unicode.IsControl) {
		return false
	}
	for _, part := range strings.Split(key, "/") {
		if part == "." || part == ".." {
			return false
		}
	}
	return true
}

func (s *DocumentFileService) readDocument(ctx context.Context, doc *entity.Document) (*DocumentFile, error) {
	var links []entity.File2Document
	if err := s.db.WithContext(ctx).Where("document_id=?", doc.ID).Limit(2).Find(&links).Error; err != nil {
		return nil, err
	}
	if len(links) > 1 {
		return nil, ErrDocumentFileUnavailable
	}
	bucket, key := doc.KbID, ""
	if doc.Location != nil {
		key = *doc.Location
	}
	if len(links) == 1 {
		if links[0].FileID == nil {
			return nil, ErrDocumentFileUnavailable
		}
		var file entity.File
		err := s.db.WithContext(ctx).Where("id=?", *links[0].FileID).Take(&file).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrDocumentFileUnavailable
		}
		if err != nil {
			return nil, err
		}
		if file.SourceType == "" || file.SourceType == "local" {
			var folder entity.File
			err = s.db.WithContext(ctx).Where("id=? AND tenant_id=? AND type=?", file.ParentID, file.TenantID, "folder").Take(&folder).Error
			if file.TenantID == "" || errors.Is(err, gorm.ErrRecordNotFound) {
				return nil, ErrDocumentFileUnavailable
			}
			if err != nil {
				return nil, err
			}
			bucket, key = folder.ID, ""
			if file.Location != nil {
				key = *file.Location
			}
		}
	}
	if !safeStorageAddress(bucket, key) {
		return nil, ErrDocumentFileUnavailable
	}
	data, err := s.readObject(ctx, bucket, key)
	if err != nil {
		return nil, err
	}
	name := ""
	if doc.Name != nil {
		name = *doc.Name
	}
	mime := utility.CONTENT_TYPE_MAP[utility.GetFileExtension(name)]
	if mime == "" {
		mime = "application/octet-stream"
	}
	return &DocumentFile{Data: data, FileName: name, ContentType: mime}, nil
}

var artifactName = regexp.MustCompile(`^[0-9a-f]{32}\.(png|jpg|jpeg|svg|pdf|csv|json|html)$`)
var artifactRunID = regexp.MustCompile(`^[0-9a-f]{32}$`)

// GetDocumentArtifact reads Python CodeExec ownership records without executing plugins.
func (s *DocumentFileService) GetDocumentArtifact(ctx context.Context, userID, filename, runID string, sessionID *string) (*DocumentFile, error) {
	if !artifactName.MatchString(filename) || !artifactRunID.MatchString(runID) || userID == "" || (sessionID != nil && !artifactRunID.MatchString(*sessionID)) {
		return nil, ErrDocumentFileUnavailable
	}
	reader := s.bindingReader
	if reader == nil {
		redisClient := cache.Get()
		if redisClient == nil {
			return nil, ErrDocumentFileStorage
		}
		reader = func(ctx context.Context, key string) (string, error) {
			return redisClient.GetClient().Get(ctx, key).Result()
		}
	}
	raw, err := reader(ctx, "multirag:sandbox_artifact:"+filename)
	if err != nil {
		// Missing ownership must never authorize an object; backend failures stay failures.
		if errors.Is(err, redis.Nil) {
			return nil, ErrDocumentFileUnavailable
		}
		return nil, ErrDocumentFileStorage
	}
	var fields map[string]json.RawMessage
	var binding artifactBinding
	if json.Unmarshal([]byte(raw), &fields) != nil || fields["session_id"] == nil || json.Unmarshal([]byte(raw), &binding) != nil || binding.OwnerID != userID || binding.RunID != runID || !sameArtifactSession(binding.SessionID, sessionID) {
		return nil, ErrDocumentFileUnavailable
	}
	if sessionID != nil {
		if s.db == nil {
			return nil, ErrDocumentFileStorage
		}
		var conversation entity.API4Conversation
		err = s.db.WithContext(ctx).Where("id=? AND source=?", *sessionID, "agent").Take(&conversation).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrDocumentFileUnavailable
		}
		if err != nil {
			return nil, err
		}
		_, err = dao.NewUserCanvasVersionDAO().AccessibleCanvas(ctx, userID, conversation.DialogID)
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrDocumentFileUnavailable
		}
		if err != nil {
			return nil, err
		}
	}
	bucket := os.Getenv("SANDBOX_ARTIFACT_BUCKET")
	if bucket == "" {
		bucket = "sandbox-artifacts"
	}
	data, err := s.readObject(ctx, bucket, filename)
	if err != nil {
		return nil, err
	}
	mime := utility.CONTENT_TYPE_MAP[utility.GetFileExtension(filename)]
	if mime == "" {
		return nil, fmt.Errorf("artifact content type unavailable")
	}
	return &DocumentFile{Data: data, FileName: filename, ContentType: mime}, nil
}

func sameArtifactSession(a, b *string) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}
