package entity

import (
	"database/sql/driver"
	"encoding/json"
	"fmt"
)

// SnapshotEntry persists historical hierarchy and the exact immutable object identity.
type SnapshotEntry struct {
	Name     string `json:"name"`
	ParentID string `json:"parent_id"`
	Type     string `json:"type"`
	Hash     string `json:"hash,omitempty"`
	Location string `json:"location,omitempty"`
	Bucket   string `json:"bucket,omitempty"`
	Size     int64  `json:"size"`
	Status   string `json:"status"`
}

type SnapshotState map[string]SnapshotEntry

func (s SnapshotState) Value() (driver.Value, error) { b, e := json.Marshal(s); return string(b), e }
func (s *SnapshotState) Scan(value any) error {
	var b []byte
	switch v := value.(type) {
	case []byte:
		b = v
	case string:
		b = []byte(v)
	default:
		return fmt.Errorf("invalid snapshot storage type")
	}
	return json.Unmarshal(b, s)
}

type FileCommit struct {
	ID        string        `gorm:"column:id;primaryKey;size:32" json:"id"`
	TenantID  string        `gorm:"column:tenant_id;size:32;not null;uniqueIndex:uq_file_commits_scope_sequence,priority:1" json:"-"`
	FolderID  string        `gorm:"column:folder_id;size:32;not null;uniqueIndex:uq_file_commits_scope_sequence,priority:2" json:"folder_id"`
	ParentID  *string       `gorm:"column:parent_id;size:32" json:"parent_id"`
	Sequence  int           `gorm:"column:sequence;not null;check:ck_file_commits_sequence,sequence >= 1;uniqueIndex:uq_file_commits_scope_sequence,priority:3" json:"sequence"`
	Message   string        `gorm:"column:message;size:512;not null" json:"message"`
	AuthorID  string        `gorm:"column:author_id;size:32;not null" json:"author_id"`
	FileCount int           `gorm:"column:file_count;not null;check:ck_file_commits_file_count,file_count >= 0" json:"file_count"`
	TreeState SnapshotState `gorm:"column:tree_state;type:jsonb;not null" json:"tree_state,omitempty"`
	BaseModel
}

func (FileCommit) TableName() string { return "t_ai_file_commits" }

type FileCommitItem struct {
	ID          string      `gorm:"column:id;primaryKey;size:32" json:"id"`
	CommitID    string      `gorm:"column:commit_id;size:32;not null;uniqueIndex:uq_file_commit_items_commit_file,priority:1" json:"commit_id"`
	FileID      string      `gorm:"column:file_id;size:32;not null;uniqueIndex:uq_file_commit_items_commit_file,priority:2" json:"file_id"`
	Operation   string      `gorm:"column:operation;size:16;not null" json:"operation"`
	OldHash     *string     `gorm:"column:old_hash;size:64" json:"old_hash"`
	NewHash     *string     `gorm:"column:new_hash;size:64" json:"new_hash"`
	OldLocation *string     `gorm:"column:old_location;size:255" json:"old_location"`
	NewLocation *string     `gorm:"column:new_location;size:255" json:"new_location"`
	OldName     *string     `gorm:"column:old_name;size:255" json:"old_name"`
	NewName     *string     `gorm:"column:new_name;size:255" json:"new_name"`
	Commit      *FileCommit `gorm:"foreignKey:CommitID;references:ID;constraint:OnDelete:RESTRICT" json:"-"`
	BaseModel
}

func (FileCommitItem) TableName() string { return "t_ai_file_commit_items" }

type FileChange struct {
	FileID    string  `json:"file_id"`
	Operation string  `json:"operation"`
	FileName  *string `json:"file_name"`
	Content   *string `json:"content"`
	OldName   *string `json:"old_name"`
	NewName   *string `json:"new_name"`
}

type CreateFileCommit struct {
	Message         string       `json:"message"`
	Files           []FileChange `json:"files"`
	ParentID        *string      `json:"parent_id"`
	ParentSpecified bool         `json:"-"`
}

type FileCommitDiff struct {
	FileID      string  `json:"file_id"`
	FileName    string  `json:"file_name"`
	Operation   string  `json:"operation"`
	OldHash     *string `json:"old_hash"`
	NewHash     *string `json:"new_hash"`
	OldLocation *string `json:"old_location"`
	NewLocation *string `json:"new_location"`
	OldName     *string `json:"old_name"`
	NewName     *string `json:"new_name"`
}
