package skills

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"path"
	"sort"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/entity"
)

func (s *Service) Install(ctx context.Context, tenant, space, key string, p *Package) (Operation, error) {
	if err := validateKey(key); err != nil {
		return Operation{}, err
	}
	if s.Blobs == nil {
		return Operation{}, fault(503, "STORAGE_UNAVAILABLE")
	}
	payload := JSON{"space_id": space, "name": p.Manifest.Name, "version": p.Manifest.Version, "content_digest": p.Digest, "activate": p.Manifest.Activate}
	op := makeOperation(tenant, "install", key, payload)
	op.Payload["skipped_binary_count"] = p.BinaryCount
	op.Payload["description"] = p.Description
	op.Payload["tags"] = p.Tags
	op.Phase = "staging"
	op.SpaceID = &space
	var version Version
	var bindings []VersionFile
	existing := false
	var err error
	for attempt := 0; attempt < 3; attempt++ {
		err = s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
			old, e := existingOperation(tx, tenant, "install", key, op.RequestHash)
			if e != nil {
				return e
			}
			if old != nil {
				op = *old
				existing = true
				return nil
			}
			reused, e := s.reuseInstall(tx, tenant, space, key, op.RequestHash, p)
			if e != nil {
				return e
			}
			if reused != nil {
				op = *reused
				existing = true
				return nil
			}
			sp, e := spaceIn(tx.Clauses(clause.Locking{Strength: "UPDATE"}), tenant, space, true)
			if e != nil {
				return e
			}
			var skill Skill
			e = tx.Where("tenant_id = ? AND space_id = ? AND name = ? AND deleted_at IS NULL", tenant, space, p.Manifest.Name).First(&skill).Error
			if errors.Is(e, gorm.ErrRecordNotFound) {
				folder, e := createFolder(tx, tenant, sp.RootFolderID, p.Manifest.Name, "skill")
				if e != nil {
					return e
				}
				tags := entity.JSONSlice{}
				for _, tag := range p.Tags {
					tags = append(tags, tag)
				}
				skill = Skill{ID: newID(), TenantID: tenant, SpaceID: space, FolderID: folder, Name: p.Manifest.Name, Description: p.Description, Tags: tags, State: "active", Revision: 1, Times: nowTimes()}
				if e = tx.Create(&skill).Error; e != nil {
					return e
				}
			} else if e != nil {
				return e
			}
			if skill.State != "active" {
				return fault(409, "RESOURCE_BUSY")
			}
			e = tx.Where("tenant_id = ? AND skill_id = ? AND version = ?", tenant, skill.ID, p.Manifest.Version).First(&version).Error
			if e == nil {
				return errInstallRace
			}
			if !errors.Is(e, gorm.ErrRecordNotFound) {
				return e
			}
			folder, e := createFolder(tx, tenant, skill.FolderID, p.Manifest.Version, "skill_version")
			if e != nil {
				return e
			}
			raw, _ := json.Marshal(p.Manifest.Files)
			var fs []any
			json.Unmarshal(raw, &fs)
			version = Version{ID: newID(), TenantID: tenant, SkillID: skill.ID, FolderID: folder, Version: p.Manifest.Version, ContentDigest: p.Digest, Manifest: JSON{"files": fs}, SourceKind: "local", State: "staging", IndexState: "unindexed", FileCount: len(p.Manifest.Files), TotalSize: p.Total, Times: nowTimes()}
			if e = tx.Create(&version).Error; e != nil {
				return e
			}
			op.ResourceID = &version.ID
			op.Payload["version_id"] = version.ID
			op.Payload["skill_id"] = skill.ID
			op.Progress = JSON{"completed": 0, "total": len(p.Manifest.Files)}
			// RequestHash intentionally covers canonical request data, not generated identifiers.
			objects := []JSON{}
			dirs := map[string]string{".": folder}
			for _, entry := range p.Manifest.Files {
				parentPath := path.Dir(entry.Path)
				parent := folder
				if parentPath != "." {
					pieces := strings.Split(parentPath, "/")
					prefix := ""
					for _, piece := range pieces {
						if prefix != "" {
							prefix += "/"
						}
						prefix += piece
						id, ok := dirs[prefix]
						if !ok {
							id, e = createFolder(tx, tenant, parent, piece, "skill_file")
							if e != nil {
								return e
							}
							dirs[prefix] = id
						}
						parent = id
					}
				}
				id := newID()
				location := op.ID + "/" + id
				n := time.Now().UTC()
				ms := n.UnixMilli()
				f := entity.File{ID: id, ParentID: parent, TenantID: tenant, CreatedBy: tenant, Name: path.Base(entry.Path), Location: &location, Size: entry.Size, Type: "other", SourceType: "skill_file", BaseModel: entity.BaseModel{CreateTime: &ms, UpdateTime: &ms, CreateDate: &n, UpdateDate: &n}}
				if e = tx.Create(&f).Error; e != nil {
					return e
				}
				binding := VersionFile{ID: newID(), TenantID: tenant, VersionID: version.ID, FileID: id, RelativePath: entry.Path, ContentDigest: entry.SHA256, Size: entry.Size, MediaType: http.DetectContentType(p.Data[entry.Path]), Times: nowTimes()}
				if e = tx.Create(&binding).Error; e != nil {
					return e
				}
				bindings = append(bindings, binding)
				objects = append(objects, JSON{"path": entry.Path, "bucket": parent, "key": location, "file_id": id, "sha256": entry.SHA256, "size": entry.Size})
			}
			op.Payload["objects"] = objects
			if e = bump(tx, space); e != nil {
				return e
			}
			return tx.Create(&op).Error
		})
		if !errors.Is(err, errInstallRace) {
			break
		}
	}
	if err != nil || existing {
		return op, err
	}
	for i, b := range bindings {
		var f entity.File
		err = s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", b.FileID, tenant).First(&f).Error
		if err == nil {
			err = s.Blobs.Put(f.ParentID, *f.Location, p.Data[b.RelativePath])
		}
		if err == nil {
			var data []byte
			data, err = s.Blobs.Get(f.ParentID, *f.Location)
			if err == nil && !bytes.Equal(data, p.Data[b.RelativePath]) {
				err = fault(503, "STORAGE_READBACK_FAILED")
			}
		}
		if err != nil {
			failure := JSON{"error_code": "STORAGE_WRITE_FAILED", "message": "Storage write failed", "retryable": false}
			s.DB.WithContext(context.WithoutCancel(ctx)).Table(Operation{}.TableName()).Where("id = ? AND phase = 'staging'", op.ID).Updates(map[string]any{"state": "failed", "error": failure})
			s.DB.WithContext(context.WithoutCancel(ctx)).Model(&Version{}).Where("id = ?", version.ID).Update("state", "install_failed")
			return op, fault(503, "STORAGE_WRITE_FAILED")
		}
		if e := s.DB.WithContext(ctx).Table(Operation{}.TableName()).Where("id = ? AND phase = 'staging' AND state = 'pending'", op.ID).Updates(map[string]any{"progress": JSON{"completed": i + 1, "total": len(bindings)}, "update_date": time.Now().UTC(), "update_time": time.Now().UTC().UnixMilli()}).Error; e != nil {
			return op, e
		}
	}
	err = s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if _, e := spaceIn(tx, tenant, space, true); e != nil {
			return e
		}
		if e := tx.Model(&Version{}).Where("id = ? AND state = 'staging'", version.ID).Update("state", "installed").Error; e != nil {
			return e
		}
		r := tx.Table(Operation{}.TableName()).Where("id = ? AND phase = 'staging' AND state = 'pending'", op.ID).Update("phase", "sealed")
		if r.Error != nil {
			return r.Error
		}
		if r.RowsAffected != 1 {
			return fault(409, "OPERATION_CONFLICT")
		}
		return nil
	})
	if err != nil {
		return op, err
	}
	return s.Operation(ctx, tenant, op.ID)
}
func (s *Service) version(ctx context.Context, tenant, space, id string) (Version, error) {
	var v Version
	if _, e := s.Space(ctx, tenant, space); e != nil {
		return v, e
	}
	e := s.DB.WithContext(ctx).Joins("JOIN t_ai_skills s ON s.id = t_ai_skill_versions.skill_id").Where("t_ai_skill_versions.id = ? AND t_ai_skill_versions.tenant_id = ? AND s.space_id = ? AND s.state = 'active' AND t_ai_skill_versions.state = 'installed'", id, tenant, space).First(&v).Error
	return v, missing(e)
}
func (s *Service) Files(ctx context.Context, tenant, space, id string) ([]VersionFile, error) {
	version, err := s.version(ctx, tenant, space, id)
	if err != nil {
		return nil, err
	}
	out := []VersionFile{}
	e := s.DB.WithContext(ctx).Where("tenant_id = ? AND version_id = ?", tenant, id).Order("relative_path").Find(&out).Error
	if e != nil {
		return nil, e
	}
	sort.Slice(out, func(i, j int) bool { return out[i].RelativePath < out[j].RelativePath })
	return out, verifyManifest(version, out)
}
func (s *Service) readFile(ctx context.Context, tenant string, b VersionFile) ([]byte, error) {
	if s.Blobs == nil {
		return nil, fault(503, "STORAGE_UNAVAILABLE")
	}
	var f entity.File
	e := s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", b.FileID, tenant).First(&f).Error
	if e != nil {
		return nil, e
	}
	if f.Location == nil {
		return nil, fault(503, "STORAGE_UNAVAILABLE")
	}
	data, e := s.Blobs.Get(f.ParentID, *f.Location)
	if e != nil {
		return nil, e
	}
	sum := sha256.Sum256(data)
	if int64(len(data)) != b.Size || hex.EncodeToString(sum[:]) != b.ContentDigest {
		return nil, fault(503, "STORAGE_READBACK_FAILED")
	}
	return data, nil
}

func verifyManifest(version Version, bindings []VersionFile) error {
	raw, e := json.Marshal(version.Manifest["files"])
	if e != nil {
		return fault(503, "CONTENT_INTEGRITY")
	}
	var expected []FileEntry
	if json.Unmarshal(raw, &expected) != nil || len(expected) != len(bindings) || len(bindings) != version.FileCount {
		return fault(503, "CONTENT_INTEGRITY")
	}
	byPath := map[string]VersionFile{}
	for _, b := range bindings {
		byPath[b.RelativePath] = b
	}
	sort.Slice(expected, func(i, j int) bool { return expected[i].Path < expected[j].Path })
	h := sha256.New()
	var size int64
	for _, f := range expected {
		b, ok := byPath[f.Path]
		if !ok || b.ContentDigest != f.SHA256 || b.Size != f.Size {
			return fault(503, "CONTENT_INTEGRITY")
		}
		delete(byPath, f.Path)
		size += f.Size
		fmt.Fprintf(h, "%s\x00%s\x00%d\n", f.Path, f.SHA256, f.Size)
	}
	if len(byPath) != 0 || size != version.TotalSize || hex.EncodeToString(h.Sum(nil)) != version.ContentDigest {
		return fault(503, "CONTENT_INTEGRITY")
	}
	return nil
}

var errInstallRace = errors.New("concurrent version installation")

// The operation row is locked before the space row, matching worker publication ordering.
func (s *Service) reuseInstall(tx *gorm.DB, tenant, space, key, hash string, p *Package) (*Operation, error) {
	var sk Skill
	e := tx.Where("tenant_id = ? AND space_id = ? AND name = ? AND deleted_at IS NULL", tenant, space, p.Manifest.Name).First(&sk).Error
	if errors.Is(e, gorm.ErrRecordNotFound) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	if sk.State != "active" {
		return nil, fault(409, "RESOURCE_BUSY")
	}
	var version Version
	e = tx.Where("tenant_id = ? AND skill_id = ? AND version = ?", tenant, sk.ID, p.Manifest.Version).First(&version).Error
	if errors.Is(e, gorm.ErrRecordNotFound) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	if version.ContentDigest != p.Digest || version.State == "deleted" {
		return nil, fault(409, "VERSION_CONFLICT")
	}
	var original Operation
	if e = tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("tenant_id = ? AND kind = 'install' AND resource_id = ?", tenant, version.ID).Order("create_time,id").First(&original).Error; e != nil {
		return nil, fault(409, "VERSION_ALREADY_INSTALLED")
	}
	if original.BackendOwner != "go" {
		return nil, fault(409, "BACKEND_OWNER_MISMATCH")
	}
	if _, e = spaceIn(tx, tenant, space, true); e != nil {
		return nil, e
	}
	if original.Payload["activate"] != p.Manifest.Activate {
		return nil, fault(409, "VERSION_ALREADY_INSTALLED")
	}
	alias := JSON{key: hash}
	if e = tx.Table(Operation{}.TableName()).Where("id = ?", original.ID).Update("payload", gorm.Expr("jsonb_set(payload, '{idempotency_aliases}', COALESCE(payload->'idempotency_aliases', '{}'::jsonb) || ?::jsonb)", alias)).Error; e != nil {
		return nil, e
	}
	return &original, nil
}
