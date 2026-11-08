package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/storage"
)

const snapshotMaxFiles = 1000
const snapshotMaxBytes = 32 * 1024 * 1024

type FileCommitError struct {
	Status  int
	Message string
}

func (e *FileCommitError) Error() string             { return e.Message }
func snapshotError(status int, message string) error { return &FileCommitError{status, message} }

type snapshotStore interface {
	GetContext(context.Context, string, string, ...string) ([]byte, error)
	Put(string, string, []byte, ...string) error
	RemoveContext(context.Context, string, string, ...string) error
	ObjectExistsContext(context.Context, string, string) (bool, error)
}
type FileCommitService struct {
	DB    *gorm.DB
	Store snapshotStore
}

func NewFileCommitService() *FileCommitService { return &FileCommitService{} }
func (s *FileCommitService) database() *gorm.DB {
	if s.DB != nil {
		return s.DB
	}
	return dao.DB
}
func (s *FileCommitService) store() (snapshotStore, error) {
	if s.Store != nil {
		return s.Store, nil
	}
	st, ok := storage.GetStorageFactory().GetStorage().(snapshotStore)
	if !ok {
		return nil, snapshotError(503, "Storage lacks strict readback")
	}
	return st, nil
}
func snapshotHash(data []byte) string { h := sha256.Sum256(data); return hex.EncodeToString(h[:]) }
func snapshotID() string              { return strings.ReplaceAll(uuid.NewString(), "-", "") }
func snapshotPtr(s string) *string    { return &s }
func snapshotRole(db *gorm.DB, tenant, user string) (string, error) {
	var m entity.UserTenant
	err := db.Where("tenant_id = ? AND user_id = ? AND status = ? AND role IN ?", tenant, user, "1", []string{"owner", "admin", "normal"}).First(&m).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return "", snapshotError(404, "Workspace not found")
	}
	return m.Role, err
}
func snapshotDataset(db *gorm.DB, id, tenant, user string, write bool) (*entity.Knowledgebase, error) {
	var kb entity.Knowledgebase
	err := db.Where("id = ? AND tenant_id = ? AND status = ?", id, tenant, "1").First(&kb).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, snapshotError(404, "Dataset not found")
	}
	if err != nil {
		return nil, err
	}
	role, err := snapshotRole(db, tenant, user)
	if err != nil {
		return nil, err
	}
	if kb.Permission != "me" && kb.Permission != "team" {
		return nil, snapshotError(404, "Dataset not found")
	}
	if role != "owner" && role != "admin" && kb.CreatedBy != user && kb.Permission != "team" {
		return nil, snapshotError(404, "Dataset not found")
	}
	if write && role != "owner" && role != "admin" && kb.CreatedBy != user {
		return nil, snapshotError(403, "Dataset write permission required")
	}
	return &kb, nil
}
func snapshotAncestors(db *gorm.DB, id, tenant string) ([]entity.File, error) {
	result := []entity.File{}
	seen := map[string]bool{}
	for len(result) < 100 && !seen[id] {
		seen[id] = true
		var f entity.File
		err := db.Where("id = ? AND tenant_id = ?", id, tenant).First(&f).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, snapshotError(404, "Workspace not found")
		}
		if err != nil {
			return nil, err
		}
		result = append(result, f)
		if f.ID == f.ParentID {
			return result, nil
		}
		id = f.ParentID
	}
	return nil, snapshotError(409, "Invalid workspace ancestry")
}
func snapshotSkill(db *gorm.DB, id string) (bool, error) {
	// Both the Python private directory skill and Go directory skill own their lifecycle.
	// The Python table is guaranteed by the common schema migration chain.
	var count int64
	var err error
	if db.Migrator().HasTable("t_ai_python_skill_spaces") {
		err = db.Table("t_ai_python_skill_spaces").Where("folder_id = ? AND state != ?", id, "deleted").Count(&count).Error
	}
	if err != nil {
		return false, err
	}
	if count > 0 {
		return true, nil
	}
	if db.Migrator().HasTable(&entity.SkillSpace{}) {
		err = db.Model(&entity.SkillSpace{}).Where("folder_id = ?", id).Count(&count).Error
	}
	return count > 0, err
}
func (s *FileCommitService) Resolve(ctx context.Context, db *gorm.DB, prefix, id, user string, write, lock bool) (*entity.File, error) {
	db = db.WithContext(ctx)
	var folder entity.File
	if prefix == "datasets" {
		var kb entity.Knowledgebase
		if err := db.Where("id = ?", id).First(&kb).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return nil, snapshotError(404, "Dataset not found")
			}
			return nil, err
		}
		if _, err := snapshotDataset(db, id, kb.TenantID, user, write); err != nil {
			return nil, err
		}
		rows := []entity.File{}
		if err := db.Where("tenant_id = ? AND name = ? AND source_type = ? AND type = ?", kb.TenantID, kb.Name, "knowledgebase", "folder").Find(&rows).Error; err != nil {
			return nil, err
		}
		if len(rows) != 1 {
			return nil, snapshotError(409, "Dataset folder mapping is missing or ambiguous")
		}
		folder = rows[0]
	} else if prefix == "folders" || prefix == "workspaces" || prefix == "workspace" {
		err := db.Where("id = ?", id).First(&folder).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, snapshotError(404, "Workspace not found")
		}
		if err != nil {
			return nil, err
		}
	} else {
		return nil, snapshotError(404, "Workspace not found")
	}
	if _, err := snapshotRole(db, folder.TenantID, user); err != nil {
		return nil, err
	}
	ancestors, err := snapshotAncestors(db, folder.ID, folder.TenantID)
	if err != nil {
		return nil, err
	}
	if lock {
		var root entity.File
		if err := db.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND tenant_id = ?", ancestors[len(ancestors)-1].ID, folder.TenantID).First(&root).Error; err != nil {
			return nil, err
		}
		ancestors, err = snapshotAncestors(db, folder.ID, folder.TenantID)
		if err != nil {
			return nil, err
		}
	}
	if ancestors[0].Type != "folder" {
		return nil, snapshotError(404, "Workspace not found")
	}
	for _, f := range ancestors {
		skill, err := snapshotSkill(db, f.ID)
		if err != nil {
			return nil, err
		}
		if skill {
			return nil, snapshotError(409, "Use the Skills lifecycle for this folder")
		}
		if f.SourceType == "knowledgebase" && f.Name != ".knowledgebase" {
			rows := []entity.Knowledgebase{}
			if err := db.Where("tenant_id = ? AND name = ? AND status = ?", f.TenantID, f.Name, "1").Find(&rows).Error; err != nil {
				return nil, err
			}
			if len(rows) != 1 {
				return nil, snapshotError(409, "Dataset folder mapping is ambiguous")
			}
			if _, err := snapshotDataset(db, rows[0].ID, f.TenantID, user, write); err != nil {
				return nil, err
			}
		} else if f.SourceType != "" && f.SourceType != "knowledgebase" {
			return nil, snapshotError(409, "Managed folders require their own lifecycle")
		}
	}
	return &ancestors[0], nil
}
func snapshotRows(db *gorm.DB, folder *entity.File) (map[string]*entity.File, error) {
	rows := map[string]*entity.File{folder.ID: folder}
	pending := []string{folder.ID}
	for len(pending) > 0 {
		children := []*entity.File{}
		if err := db.Where("parent_id IN ? AND id != parent_id", pending).Order("id").Find(&children).Error; err != nil {
			return nil, err
		}
		pending = nil
		for _, f := range children {
			if f.TenantID != folder.TenantID || rows[f.ID] != nil {
				return nil, snapshotError(409, "Invalid workspace ancestry")
			}
			if f.Type == "folder" && f.SourceType != "" {
				return nil, snapshotError(409, "Snapshot managed folders through their own scope")
			}
			skill, err := snapshotSkill(db, f.ID)
			if err != nil {
				return nil, err
			}
			if skill {
				return nil, snapshotError(409, "Use the Skills lifecycle for this folder")
			}
			rows[f.ID] = f
			if f.Type == "folder" {
				pending = append(pending, f.ID)
			}
		}
		if len(rows) > snapshotMaxFiles {
			return nil, snapshotError(413, "Workspace contains too many files")
		}
	}
	return rows, nil
}
func snapshotLive(ctx context.Context, db *gorm.DB, st snapshotStore, folder *entity.File, rows map[string]*entity.File) (entity.SnapshotState, map[string][]byte, error) {
	state := entity.SnapshotState{}
	contents := map[string][]byte{}
	total := 0
	for id, f := range rows {
		e := entity.SnapshotEntry{Name: f.Name, ParentID: f.ParentID, Type: "folder", Status: "1"}
		if f.Type != "folder" {
			e.Type = "file"
			bucket := f.ParentID
			key := ""
			if f.Location != nil {
				key = *f.Location
			}
			if f.SourceType != "" {
				if f.SourceType != "knowledgebase" || folder.SourceType != "knowledgebase" {
					return nil, nil, snapshotError(409, "Managed file content cannot be resolved")
				}
				links := []entity.File2Document{}
				if err := db.Where("file_id = ?", id).Find(&links).Error; err != nil {
					return nil, nil, err
				}
				if len(links) != 1 || links[0].DocumentID == nil {
					return nil, nil, snapshotError(409, "Dataset document is missing")
				}
				var doc entity.Document
				if err := db.Where("id = ?", *links[0].DocumentID).First(&doc).Error; err != nil {
					return nil, nil, err
				}
				var kb entity.Knowledgebase
				if err := db.Where("id = ? AND tenant_id = ? AND name = ?", doc.KbID, folder.TenantID, folder.Name).First(&kb).Error; err != nil {
					return nil, nil, snapshotError(404, "File not found")
				}
				bucket = kb.ID
				if doc.Location != nil {
					key = *doc.Location
				}
			}
			if key == "" {
				return nil, nil, snapshotError(503, "File content is unavailable")
			}
			data, err := st.GetContext(ctx, bucket, key)
			if err != nil {
				return nil, nil, err
			}
			if data == nil {
				return nil, nil, snapshotError(503, "File content is unavailable")
			}
			total += len(data)
			if total > snapshotMaxBytes {
				return nil, nil, snapshotError(413, "Workspace content exceeds limit")
			}
			e.Hash = snapshotHash(data)
			e.Size = int64(len(data))
			e.Bucket = bucket
			e.Location = key
			contents[id] = data
		}
		state[id] = e
	}
	return state, contents, nil
}
func DiffSnapshotStates(old, new entity.SnapshotState) []entity.FileCommitDiff {
	ids := map[string]bool{}
	for id := range old {
		ids[id] = true
	}
	for id := range new {
		ids[id] = true
	}
	ordered := []string{}
	for id := range ids {
		ordered = append(ordered, id)
	}
	sort.Strings(ordered)
	result := []entity.FileCommitDiff{}
	for _, id := range ordered {
		a, hasA := old[id]
		b, hasB := new[id]
		op := ""
		switch {
		case !hasA:
			op = "add"
		case !hasB:
			op = "delete"
		case a.Hash != b.Hash || a.Size != b.Size || a.Type != b.Type:
			op = "modify"
		case a.Name != b.Name || a.ParentID != b.ParentID:
			op = "rename"
		}
		if op != "" {
			d := entity.FileCommitDiff{FileID: id, FileName: b.Name, Operation: op}
			if !hasB {
				d.FileName = a.Name
			}
			if hasA {
				d.OldHash = snapshotPtr(a.Hash)
				d.OldLocation = snapshotPtr(a.Location)
				d.OldName = snapshotPtr(a.Name)
			}
			if hasB {
				d.NewHash = snapshotPtr(b.Hash)
				d.NewLocation = snapshotPtr(b.Location)
				d.NewName = snapshotPtr(b.Name)
			}
			result = append(result, d)
		}
	}
	return result
}
func SnapshotTree(state entity.SnapshotState, root string) (map[string]any, error) {
	seen := map[string]bool{}
	var visit func(string) (map[string]any, error)
	visit = func(id string) (map[string]any, error) {
		e, ok := state[id]
		if !ok || seen[id] {
			return nil, snapshotError(503, "Snapshot ancestry is invalid")
		}
		seen[id] = true
		n := map[string]any{"id": id, "name": e.Name, "type": e.Type, "parent_id": e.ParentID, "size": e.Size, "status": e.Status}
		if e.Type == "file" {
			n["hash"] = e.Hash
			n["location"] = e.Location
			n["bucket"] = e.Bucket
			return n, nil
		}
		ids := []string{}
		for child, v := range state {
			if child != root && v.ParentID == id {
				ids = append(ids, child)
			}
		}
		sort.Strings(ids)
		children := []any{}
		for _, child := range ids {
			n, err := visit(child)
			if err != nil {
				return nil, err
			}
			children = append(children, n)
		}
		n["children"] = children
		return n, nil
	}
	n, err := visit(root)
	if err != nil {
		return nil, err
	}
	if len(seen) != len(state) {
		return nil, snapshotError(503, "Snapshot has unreachable entries")
	}
	return n, nil
}
func validateSnapshotRequest(req entity.CreateFileCommit) error {
	if !utf8.ValidString(req.Message) || utf8.RuneCountInString(req.Message) > 512 || req.Files == nil || len(req.Files) > snapshotMaxFiles {
		return snapshotError(422, "Invalid commit request")
	}
	ids := map[string]bool{}
	total := 0
	for _, c := range req.Files {
		if len(c.FileID) == 0 || len(c.FileID) > 32 || ids[c.FileID] {
			return snapshotError(422, "Invalid or duplicate file ID")
		}
		ids[c.FileID] = true
		switch c.Operation {
		case "add":
			if c.FileName == nil || c.Content == nil {
				return snapshotError(422, "Name and content required")
			}
		case "modify":
			if c.Content == nil {
				return snapshotError(422, "Content required")
			}
		case "rename":
			if c.NewName == nil || c.Content != nil {
				return snapshotError(422, "New name required")
			}
		case "delete":
			if c.Content != nil {
				return snapshotError(422, "Content only accepted for add/modify")
			}
		default:
			return snapshotError(422, "Invalid file operation")
		}
		for _, name := range []*string{c.FileName, c.NewName} {
			if name != nil && (*name == "" || utf8.RuneCountInString(*name) > 255 || *name == "." || *name == ".." || strings.ContainsAny(*name, "/\\\x00")) {
				return snapshotError(422, "Invalid file name")
			}
		}
		if c.Content != nil {
			total += len(*c.Content)
		}
	}
	if total > snapshotMaxBytes {
		return snapshotError(413, "Commit content exceeds limit")
	}
	return nil
}

// cleanupSnapshotObjects attempts every reserved key even when an earlier
// removal or independent existence check fails. The caller retains all causes.
func cleanupSnapshotObjects(ctx context.Context, st snapshotStore, written [][2]string) error {
	failures := []error{}
	for i := len(written) - 1; i >= 0; i-- {
		object := written[i]
		if err := st.RemoveContext(ctx, object[0], object[1]); err != nil {
			failures = append(failures, err)
		}
		exists, err := st.ObjectExistsContext(ctx, object[0], object[1])
		if err != nil {
			failures = append(failures, err)
		} else if exists {
			failures = append(failures, snapshotError(503, "Reserved snapshot object remains"))
		}
	}
	return errors.Join(failures...)
}

func (s *FileCommitService) Create(ctx context.Context, prefix, id, user string, req entity.CreateFileCommit) (*entity.FileCommit, error) {
	if err := validateSnapshotRequest(req); err != nil {
		return nil, err
	}
	st, err := s.store()
	if err != nil {
		return nil, err
	}
	db := s.database().WithContext(ctx)
	tx := db.Begin()
	defer tx.Rollback()
	if tx.Error != nil {
		return nil, tx.Error
	}
	written := [][2]string{}
	committing := false
	commitID := snapshotID()
	fail := func(cause error) (*entity.FileCommit, error) {
		tx.Rollback()
		if committing {
			var count int64
			readCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if err := s.database().WithContext(readCtx).Model(&entity.FileCommit{}).Where("id = ?", commitID).Count(&count).Error; err != nil {
				return nil, snapshotError(503, "Commit outcome requires recovery; objects retained")
			}
			if count > 0 {
				return nil, snapshotError(503, "Commit persisted; reload history before retrying")
			}
		}
		cleanCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if cleanupErr := cleanupSnapshotObjects(cleanCtx, st, written); cleanupErr != nil {
			return nil, errors.Join(cause, cleanupErr, snapshotError(503, "Snapshot rollback cleanup requires recovery"))
		}
		return nil, cause
	}
	folder, err := s.Resolve(ctx, tx, prefix, id, user, true, true)
	if err != nil {
		return fail(err)
	}
	d := dao.FileCommitDAO{DB: tx, TenantID: folder.TenantID, FolderID: folder.ID}
	latest, err := d.Latest(ctx)
	if err != nil {
		return fail(err)
	}
	head := ""
	sequence := 1
	old := entity.SnapshotState{}
	if latest != nil {
		head = latest.ID
		sequence = latest.Sequence + 1
		old = latest.TreeState
	}
	expected := ""
	if req.ParentID != nil {
		expected = *req.ParentID
	}
	if req.ParentSpecified && expected != head {
		return fail(snapshotError(409, "Commit head changed"))
	}
	rows, err := snapshotRows(tx, folder)
	if err != nil {
		return fail(err)
	}
	for _, c := range req.Files {
		f := rows[c.FileID]
		if c.Operation == "add" {
			var count int64
			if err := tx.Model(&entity.File{}).Where("id = ?", c.FileID).Count(&count).Error; err != nil {
				return fail(err)
			}
			_, historical := old[c.FileID]
			if count > 0 || historical {
				return fail(snapshotError(409, "File ID already exists"))
			}
		} else if f == nil || f.Type == "folder" {
			return fail(snapshotError(404, "File not found in workspace"))
		}
		if f != nil {
			var count int64
			if err := tx.Model(&entity.File2Document{}).Where("file_id = ?", f.ID).Count(&count).Error; err != nil {
				return fail(err)
			}
			if count > 0 || f.SourceType != "" {
				return fail(snapshotError(409, "Managed files require their own lifecycle"))
			}
			if c.OldName != nil && *c.OldName != f.Name {
				return fail(snapshotError(409, "File name changed"))
			}
		}
	}
	state, contents, err := snapshotLive(ctx, tx, st, folder, rows)
	if err != nil {
		return fail(err)
	}
	updates := map[string]bool{}
	for _, c := range req.Files {
		f := rows[c.FileID]
		if c.Operation == "add" {
			f = &entity.File{ID: c.FileID, ParentID: folder.ID, TenantID: folder.TenantID, CreatedBy: user, Name: *c.FileName, Type: "other"}
			rows[f.ID] = f
			state[f.ID] = entity.SnapshotEntry{Name: f.Name, ParentID: f.ParentID, Type: "file", Status: "1"}
			if err := tx.Create(f).Error; err != nil {
				return fail(err)
			}
		}
		if c.Operation == "delete" {
			if err := tx.Where("id = ? AND tenant_id = ?", f.ID, folder.TenantID).Delete(&entity.File{}).Error; err != nil {
				return fail(err)
			}
			delete(state, f.ID)
			delete(contents, f.ID)
			continue
		}
		e := state[f.ID]
		if c.Operation == "add" || c.Operation == "modify" {
			contents[f.ID] = []byte(*c.Content)
			e.Hash = snapshotHash(contents[f.ID])
			e.Size = int64(len(contents[f.ID]))
			f.Size = e.Size
			updates[f.ID] = true
		}
		if c.Operation == "rename" {
			f.Name = *c.NewName
		} else if c.FileName != nil {
			f.Name = *c.FileName
		}
		e.Name = f.Name
		state[f.ID] = e
	}
	names := map[string]bool{}
	total := 0
	for _, e := range state {
		key := e.ParentID + "\x00" + e.Name
		if names[key] {
			return fail(snapshotError(409, "Duplicate file name"))
		}
		names[key] = true
	}
	for _, b := range contents {
		total += len(b)
	}
	if total > snapshotMaxBytes || len(state) > snapshotMaxFiles {
		return fail(snapshotError(413, "Snapshot exceeds limits"))
	}
	put := func(bucket, key string, b []byte) error {
		p := [2]string{bucket, key}
		for _, done := range written {
			if p == done {
				return nil
			}
		}
		written = append(written, p)
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := st.Put(bucket, key, b); err != nil {
			return err
		}
		read, err := st.GetContext(ctx, bucket, key)
		if err != nil {
			return err
		}
		if snapshotHash(read) != snapshotHash(b) || len(read) != len(b) {
			return snapshotError(503, "Snapshot content integrity failure")
		}
		return nil
	}
	for fid, b := range contents {
		e := state[fid]
		previous, ok := old[fid]
		if ok && previous.Hash == e.Hash {
			data, err := st.GetContext(ctx, previous.Bucket, previous.Location)
			if err != nil {
				return fail(err)
			}
			if snapshotHash(data) != e.Hash || len(data) != len(b) {
				return fail(snapshotError(503, "Snapshot content integrity failure"))
			}
			e.Bucket = previous.Bucket
			e.Location = previous.Location
		} else {
			e.Bucket = rows[fid].ParentID
			e.Location = fmt.Sprintf(".file-commits/%s/%s/%s/%s", folder.TenantID, folder.ID, commitID, e.Hash)
			if err := put(e.Bucket, e.Location, b); err != nil {
				return fail(err)
			}
		}
		if updates[fid] {
			// The ordinary Files lifecycle owns only this working object;
			// it can move/delete it without retiring historical snapshot bytes.
			bucket := rows[fid].ParentID
			key := fmt.Sprintf(".file-working/%s/%s/%s/%s/%s", folder.TenantID, folder.ID, commitID, fid, e.Hash)
			if err := put(bucket, key, b); err != nil {
				return fail(err)
			}
			rows[fid].Location = snapshotPtr(key)
		}
		state[fid] = e
	}
	for _, c := range req.Files {
		if c.Operation != "delete" {
			f := rows[c.FileID]
			if err := tx.Model(&entity.File{}).Where("id = ? AND tenant_id = ?", f.ID, folder.TenantID).Updates(map[string]any{"name": f.Name, "size": f.Size, "location": f.Location}).Error; err != nil {
				return fail(err)
			}
		}
	}
	if _, err := SnapshotTree(state, folder.ID); err != nil {
		return fail(err)
	}
	changes := DiffSnapshotStates(old, state)
	items := []entity.FileCommitItem{}
	for _, c := range changes {
		if c.FileID == folder.ID {
			continue
		}
		items = append(items, entity.FileCommitItem{ID: snapshotID(), CommitID: commitID, FileID: c.FileID, Operation: c.Operation, OldHash: c.OldHash, NewHash: c.NewHash, OldLocation: c.OldLocation, NewLocation: c.NewLocation, OldName: c.OldName, NewName: c.NewName})
	}
	commit := &entity.FileCommit{ID: commitID, TenantID: folder.TenantID, FolderID: folder.ID, Sequence: sequence, Message: req.Message, AuthorID: user, FileCount: len(items), TreeState: state}
	if latest != nil {
		commit.ParentID = snapshotPtr(latest.ID)
	}
	if err := tx.Create(commit).Error; err != nil {
		return fail(err)
	}
	if len(items) > 0 {
		if err := tx.Create(&items).Error; err != nil {
			return fail(err)
		}
	}
	committing = true
	if err := tx.Commit().Error; err != nil {
		return fail(err)
	}
	return commit, nil
}
func (s *FileCommitService) Get(ctx context.Context, folder *entity.File, id string) (*entity.FileCommit, error) {
	c, err := (dao.FileCommitDAO{DB: s.database(), TenantID: folder.TenantID, FolderID: folder.ID}).Get(ctx, id)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, snapshotError(404, "Commit not found")
	}
	if err != nil {
		return nil, err
	}
	if _, err := SnapshotTree(c.TreeState, folder.ID); err != nil {
		return nil, err
	}
	return c, nil
}
func (s *FileCommitService) Content(ctx context.Context, c *entity.FileCommit, id string) ([]byte, error) {
	e, ok := c.TreeState[id]
	if !ok || e.Type != "file" {
		return nil, snapshotError(404, "File not found in commit")
	}
	if !strings.HasPrefix(e.Location, ".file-commits/"+c.TenantID+"/"+c.FolderID+"/") {
		return nil, snapshotError(503, "Snapshot object identity is invalid")
	}
	st, err := s.store()
	if err != nil {
		return nil, err
	}
	b, err := st.GetContext(ctx, e.Bucket, e.Location)
	if err != nil {
		return nil, err
	}
	if int64(len(b)) != e.Size || snapshotHash(b) != e.Hash {
		return nil, snapshotError(503, "Snapshot content integrity failure")
	}
	return b, nil
}
func (s *FileCommitService) Changes(ctx context.Context, folder *entity.File) ([]entity.FileCommitDiff, error) {
	st, err := s.store()
	if err != nil {
		return nil, err
	}
	db := s.database().WithContext(ctx)
	rows, err := snapshotRows(db, folder)
	if err != nil {
		return nil, err
	}
	state, _, err := snapshotLive(ctx, db, st, folder, rows)
	if err != nil {
		return nil, err
	}
	latest, err := (dao.FileCommitDAO{DB: db, TenantID: folder.TenantID, FolderID: folder.ID}).Latest(ctx)
	if err != nil {
		return nil, err
	}
	old := entity.SnapshotState{}
	if latest != nil {
		old = latest.TreeState
	}
	result := []entity.FileCommitDiff{}
	for _, c := range DiffSnapshotStates(old, state) {
		if c.FileID != folder.ID {
			result = append(result, c)
		}
	}
	return result, nil
}

func (s *FileCommitService) Versions(ctx context.Context, user, id string) ([]map[string]any, error) {
	db := s.database().WithContext(ctx)
	type record struct {
		entity.FileCommitItem
		FolderID, TenantID, Message string
		CommitTime                  *int64
	}
	records := []record{}
	err := db.Table("t_ai_file_commit_items AS i").Select("i.*, c.folder_id, c.tenant_id, c.message, c.create_time AS commit_time").Joins("JOIN t_ai_file_commits c ON c.id=i.commit_id").Joins("JOIN t_ai_user_tenants m ON m.tenant_id=c.tenant_id AND m.user_id=? AND m.status='1' AND m.role IN ('owner','admin','normal')", user).Where("i.file_id = ?", id).Order("c.create_time DESC, c.id DESC").Scan(&records).Error
	if err != nil {
		return nil, err
	}
	result := []map[string]any{}
	for _, r := range records {
		if _, err := s.Resolve(ctx, db, "folders", r.FolderID, user, false, false); err != nil {
			var e *FileCommitError
			if errors.As(err, &e) && (e.Status == 403 || e.Status == 404) {
				continue
			}
			return nil, err
		}
		hash := ""
		if r.NewHash != nil {
			hash = *r.NewHash
		} else if r.OldHash != nil {
			hash = *r.OldHash
		}
		result = append(result, map[string]any{"commit_id": r.CommitID, "folder_id": r.FolderID, "operation": r.Operation, "hash": hash, "message": r.Message, "create_time": r.CommitTime})
	}
	if len(result) == 0 {
		var f entity.File
		if err := db.Where("id = ?", id).First(&f).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return nil, snapshotError(404, "File not found")
			}
			return nil, err
		}
		if _, err := s.Resolve(ctx, db, "folders", f.ParentID, user, false, false); err != nil {
			return nil, err
		}
	}
	return result, nil
}
