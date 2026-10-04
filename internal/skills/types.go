// Package skills implements the shared Skills v1 contract without migrating its schema.
package skills

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"gorm.io/gorm"
	"multirag/internal/entity"
)

type JSON = entity.JSONMap
type Error struct {
	Status  int
	Code    string
	Message string
}

func (e *Error) Error() string            { return e.Code + ": " + e.Message }
func fault(status int, code string) error { return &Error{status, code, code} }
func missing(err error) error {
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return fault(404, "NOT_FOUND")
	}
	return err
}
func newID() string { return strings.ReplaceAll(uuid.NewString(), "-", "") }

type Times struct {
	CreateTime int64     `json:"create_time"`
	UpdateTime int64     `json:"update_time"`
	CreateDate time.Time `json:"-"`
	UpdateDate time.Time `json:"-"`
}

func nowTimes() Times { n := time.Now().UTC(); return Times{n.UnixMilli(), n.UnixMilli(), n, n} }

type Space struct {
	ID                 string     `gorm:"primaryKey" json:"id"`
	TenantID           string     `json:"-"`
	CreatedBy          string     `json:"-"`
	Name               string     `json:"name"`
	NameKey            string     `json:"-"`
	Description        string     `json:"description"`
	RootFolderID       string     `json:"root_folder_id"`
	State              string     `json:"state"`
	BackendOwner       string     `json:"backend_owner"`
	Revision           int64      `json:"revision"`
	ActiveGenerationID *string    `json:"active_generation_id"`
	DeletedAt          *time.Time `json:"-"`
	Times
}

func (Space) TableName() string { return "t_ai_skill_spaces" }

type Skill struct {
	ID              string           `gorm:"primaryKey" json:"id"`
	TenantID        string           `json:"-"`
	SpaceID         string           `json:"space_id"`
	FolderID        string           `json:"-"`
	Name            string           `json:"name"`
	Description     string           `json:"description"`
	Tags            entity.JSONSlice `gorm:"type:jsonb" json:"tags"`
	ActiveVersionID *string          `json:"active_version_id"`
	State           string           `json:"state"`
	Revision        int64            `json:"revision"`
	DeletedAt       *time.Time       `json:"-"`
	Times
}

func (Skill) TableName() string { return "t_ai_skills" }

type Version struct {
	ID            string     `gorm:"primaryKey" json:"id"`
	TenantID      string     `json:"-"`
	SkillID       string     `json:"skill_id"`
	FolderID      string     `json:"-"`
	Version       string     `json:"version"`
	ContentDigest string     `json:"content_digest"`
	Manifest      JSON       `gorm:"type:jsonb" json:"-"`
	SourceKind    string     `json:"-"`
	State         string     `json:"state"`
	IndexState    string     `json:"index_state"`
	FileCount     int        `json:"file_count"`
	TotalSize     int64      `json:"total_size"`
	DeletedAt     *time.Time `json:"-"`
	Times
}

func (Version) TableName() string { return "t_ai_skill_versions" }

type VersionFile struct {
	ID            string `gorm:"primaryKey" json:"-"`
	TenantID      string `json:"-"`
	VersionID     string `json:"-"`
	FileID        string `json:"-"`
	RelativePath  string `json:"path"`
	ContentDigest string `json:"sha256"`
	Size          int64  `json:"size"`
	MediaType     string `json:"media_type"`
	Times
}

func (VersionFile) TableName() string { return "t_ai_skill_version_files" }

type Config struct {
	ID                  string  `gorm:"primaryKey" json:"-"`
	TenantID            string  `json:"-"`
	SpaceID             string  `json:"-"`
	EmbeddingModelID    *int64  `json:"-"`
	RerankModelID       *int64  `json:"-"`
	TopK                int     `json:"top_k"`
	VectorWeight        float64 `json:"vector_weight"`
	SimilarityThreshold float64 `json:"similarity_threshold"`
	Fields              JSON    `gorm:"type:jsonb" json:"fields"`
	Revision            int64   `json:"revision"`
	Times
}

func (Config) TableName() string { return "t_ai_skill_search_configs" }
func (c Config) Snapshot() JSON {
	var embed, rerank any
	if c.EmbeddingModelID != nil {
		embed = fmt.Sprint(*c.EmbeddingModelID)
	}
	if c.RerankModelID != nil {
		rerank = fmt.Sprint(*c.RerankModelID)
	}
	return JSON{"embedding_model_id": embed, "rerank_model_id": rerank, "top_k": c.TopK, "vector_weight": c.VectorWeight, "similarity_threshold": c.SimilarityThreshold, "fields": c.Fields, "revision": c.Revision}
}
func (c Config) MarshalJSON() ([]byte, error) { return json.Marshal(c.Snapshot()) }
func defaultConfig(tenant, space string) Config {
	return Config{ID: newID(), TenantID: tenant, SpaceID: space, TopK: 10, VectorWeight: .3, SimilarityThreshold: .2, Revision: 1, Fields: JSON{"name": JSON{"enabled": true, "weight": 3.0}, "tags": JSON{"enabled": true, "weight": 2.0}, "description": JSON{"enabled": true, "weight": 1.0}, "content": JSON{"enabled": false, "weight": .5}}, Times: nowTimes()}
}

type Generation struct {
	ID             string `gorm:"primaryKey"`
	TenantID       string
	SpaceID        string
	ConfigRevision int64
	SourceRevision int64
	Config         JSON `gorm:"type:jsonb"`
	Dimension      int
	IndexName      string
	State          string
	Error          JSON `gorm:"type:jsonb"`
	Times
}

func (Generation) TableName() string { return "t_ai_skill_index_generations" }

type Operation struct {
	ID             string     `gorm:"primaryKey" json:"id"`
	TenantID       string     `json:"-"`
	SpaceID        *string    `json:"-"`
	ResourceID     *string    `json:"resource_id"`
	BackendOwner   string     `json:"-"`
	Kind           string     `json:"kind"`
	State          string     `json:"state"`
	Phase          string     `json:"phase"`
	IdempotencyKey string     `json:"-"`
	RequestHash    string     `json:"-"`
	Payload        JSON       `gorm:"type:jsonb" json:"-"`
	Progress       JSON       `gorm:"type:jsonb" json:"progress"`
	Result         JSON       `gorm:"type:jsonb" json:"result"`
	Error          JSON       `gorm:"type:jsonb" json:"error"`
	Attempts       int        `json:"attempts"`
	LeaseOwner     *string    `json:"-"`
	LeaseExpiresAt *time.Time `json:"-"`
	NextAttemptAt  *time.Time `json:"-"`
	Revision       int64      `json:"-"`
	Times
}

func (Operation) TableName() string { return "t_ai_skill_operations" }
func (o Operation) Accepted() JSON {
	return JSON{"operation_id": o.ID, "state": o.State, "resource_id": o.ResourceID}
}

type BlobStore interface {
	Put(string, string, []byte, ...string) error
	Get(string, string, ...string) ([]byte, error)
	Remove(string, string, ...string) error
	Exists(string, string) (bool, error)
}
type ModelResolver interface {
	Embed(context.Context, string, int64, []string, bool) ([][]float64, error)
	Rerank(context.Context, string, int64, string, []string) ([]float64, error)
	List(context.Context, string) ([]JSON, error)
	Validate(context.Context, string, int64, string) error
}
type IndexRow struct {
	ID, SkillID, VersionID, Field, Text, Digest string
	Vector                                      []float32
}
type Hit struct {
	SkillID, VersionID, Text string
	Score                    float64
}
type SearchHits struct {
	Hits      []Hit
	Truncated bool
}
type IndexStore interface {
	Build(context.Context, string, int, []IndexRow) error
	Search(context.Context, string, string, string, []float32, Config) (SearchHits, error)
	Delete(context.Context, string, []string) error
	Drop(context.Context, string) error
	Verify(context.Context, string, []IndexRow) error
}
type Service struct {
	DB     *gorm.DB
	Blobs  BlobStore
	Index  IndexStore
	Models ModelResolver
}

func New(db *gorm.DB, blobs BlobStore, index IndexStore, models ModelResolver) *Service {
	return &Service{db, blobs, index, models}
}
