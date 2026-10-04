package skills

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/entity"
)

func (s *Service) Ready(ctx context.Context) error {
	for _, table := range []string{Space{}.TableName(), Skill{}.TableName(), Version{}.TableName(), VersionFile{}.TableName(), Config{}.TableName(), Generation{}.TableName(), Operation{}.TableName()} {
		if !s.DB.WithContext(ctx).Migrator().HasTable(table) {
			return fault(503, "SCHEMA_NOT_READY")
		}
	}
	return nil
}
func spaceIn(db *gorm.DB, tenant, id string, write bool) (Space, error) {
	var v Space
	err := db.Where("id = ? AND tenant_id = ? AND state = ?", id, tenant, "active").First(&v).Error
	if err != nil {
		return v, missing(err)
	}
	if write && v.BackendOwner != "go" {
		return v, fault(409, "BACKEND_OWNER_MISMATCH")
	}
	return v, nil
}
func (s *Service) Space(ctx context.Context, tenant, id string) (Space, error) {
	return spaceIn(s.DB.WithContext(ctx), tenant, id, false)
}
func stamp() map[string]any {
	n := time.Now().UTC()
	return map[string]any{"update_time": n.UnixMilli(), "update_date": n}
}
func bump(db *gorm.DB, space string) error {
	u := stamp()
	u["revision"] = gorm.Expr("revision + 1")
	return db.Model(&Space{}).Where("id = ? AND state = 'active'", space).Updates(u).Error
}
func createFolder(db *gorm.DB, tenant, parent, name, kind string) (string, error) {
	id := newID()
	if parent == "" {
		parent = id
	}
	n := time.Now().UTC()
	ms := n.UnixMilli()
	f := entity.File{ID: id, ParentID: parent, TenantID: tenant, CreatedBy: tenant, Name: name, Type: "folder", SourceType: kind, BaseModel: entity.BaseModel{CreateTime: &ms, UpdateTime: &ms, CreateDate: &n, UpdateDate: &n}}
	return id, db.Create(&f).Error
}
func (s *Service) CreateSpace(ctx context.Context, tenant, name, description string) (Space, error) {
	name, key, err := spaceName(name)
	if err != nil {
		return Space{}, err
	}
	v := Space{ID: newID(), TenantID: tenant, CreatedBy: tenant, Name: name, NameKey: key, Description: description, State: "active", BackendOwner: "go", Revision: 1, Times: nowTimes()}
	err = s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if e := tx.Exec("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", "skills-root:"+tenant).Error; e != nil {
			return e
		}
		var root entity.File
		e := tx.Where("tenant_id = ? AND parent_id = id AND (source_type IS NULL OR source_type NOT IN ?)", tenant, []string{"skill_space", "skill", "skill_version", "skill_file"}).First(&root).Error
		if errors.Is(e, gorm.ErrRecordNotFound) {
			id, e := createFolder(tx, tenant, "", "/", "")
			if e != nil {
				return e
			}
			root.ID = id
		} else if e != nil {
			return e
		}
		id, err := createFolder(tx, tenant, root.ID, name, "skill_space")
		if err != nil {
			return err
		}
		v.RootFolderID = id
		if err = tx.Create(&v).Error; err != nil {
			return err
		}
		c := defaultConfig(tenant, v.ID)
		return tx.Create(&c).Error
	})
	if errors.Is(err, gorm.ErrDuplicatedKey) || err != nil && strings.Contains(err.Error(), "23505") {
		return v, fault(409, "NAME_CONFLICT")
	}
	return v, err
}
func (s *Service) PatchSpace(ctx context.Context, tenant, id string, revision int64, name, description *string) (Space, error) {
	var result Space
	err := s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var err error
		result, err = spaceIn(tx, tenant, id, true)
		if err != nil {
			return err
		}
		u := stamp()
		u["revision"] = revision + 1
		if name != nil {
			n, k, e := spaceName(*name)
			if e != nil {
				return e
			}
			u["name"] = n
			u["name_key"] = k
		}
		if description != nil {
			u["description"] = *description
		}
		r := tx.Model(&Space{}).Where("id = ? AND revision = ? AND state = 'active'", id, revision).Updates(u)
		if r.Error != nil {
			return r.Error
		}
		if r.RowsAffected != 1 {
			return fault(409, "REVISION_CONFLICT")
		}
		if name != nil {
			if e := tx.Model(&entity.File{}).Where("id = ? AND tenant_id = ?", result.RootFolderID, tenant).Updates(map[string]any{"name": u["name"], "update_time": time.Now().UnixMilli(), "update_date": time.Now().UTC()}).Error; e != nil {
				return e
			}
		}
		return tx.First(&result, "id = ?", id).Error
	})
	if errors.Is(err, gorm.ErrDuplicatedKey) || err != nil && strings.Contains(err.Error(), "23505") {
		return result, fault(409, "NAME_CONFLICT")
	}
	return result, err
}
func (s *Service) Config(ctx context.Context, tenant, space string) (Config, error) {
	var c Config
	if _, err := s.Space(ctx, tenant, space); err != nil {
		return c, err
	}
	err := s.DB.WithContext(ctx).Where("tenant_id = ? AND space_id = ?", tenant, space).First(&c).Error
	return c, missing(err)
}
func parseModelID(v any) (*int64, error) {
	if v == nil {
		return nil, nil
	}
	str, ok := v.(string)
	if !ok || str == "" {
		return nil, fault(422, "INVALID_MODEL_ID")
	}
	n, e := strconv.ParseInt(str, 10, 64)
	if e != nil || n <= 0 || fmt.Sprint(n) != str {
		return nil, fault(422, "INVALID_MODEL_ID")
	}
	return &n, nil
}
func number(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case json.Number:
		f, e := n.Float64()
		return f, e == nil
	}
	return 0, false
}
func validateFields(v any) (JSON, error) {
	raw, e := json.Marshal(v)
	if e != nil {
		return nil, fault(422, "INVALID_FIELDS")
	}
	var fields JSON
	if json.Unmarshal(raw, &fields) != nil || len(fields) != 4 {
		return nil, fault(422, "INVALID_FIELDS")
	}
	positive := false
	for _, name := range []string{"name", "tags", "description", "content"} {
		field := asJSON(fields[name])
		if len(field) != 2 {
			return nil, fault(422, "INVALID_FIELDS")
		}
		enabled, ok := field["enabled"].(bool)
		weight, numberOK := number(field["weight"])
		if !ok || !numberOK || weight < 0 || weight > 10 {
			return nil, fault(422, "INVALID_FIELDS")
		}
		positive = positive || enabled && weight > 0
	}
	if !positive {
		return nil, fault(422, "INVALID_FIELDS")
	}
	return fields, nil
}
func (s *Service) PatchConfig(ctx context.Context, tenant, space string, values JSON) (Config, error) {
	c, err := s.Config(ctx, tenant, space)
	if err != nil {
		return c, err
	}
	rev, ok := number(values["revision"])
	if !ok || rev != float64(int64(rev)) || int64(rev) != c.Revision {
		return c, fault(409, "REVISION_CONFLICT")
	}
	for k, v := range values {
		switch k {
		case "revision":
		case "embedding_model_id", "rerank_model_id":
			id, e := parseModelID(v)
			if e != nil {
				return c, e
			}
			typ := "embedding"
			if k == "rerank_model_id" {
				typ = "rerank"
			}
			if id != nil {
				if s.Models == nil {
					return c, fault(503, "MODEL_UNAVAILABLE")
				}
				if e = s.Models.Validate(ctx, tenant, *id, typ); e != nil {
					return c, e
				}
			}
			if k == "embedding_model_id" {
				c.EmbeddingModelID = id
			} else {
				c.RerankModelID = id
			}
		case "top_k":
			n, ok := number(v)
			if !ok || n != float64(int(n)) || n < 1 || n > 100 {
				return c, fault(422, "INVALID_TOP_K")
			}
			c.TopK = int(n)
		case "vector_weight", "similarity_threshold":
			n, ok := number(v)
			if !ok || n < 0 || n > 1 {
				return c, fault(422, "INVALID_WEIGHT")
			}
			if k == "vector_weight" {
				c.VectorWeight = n
			} else {
				c.SimilarityThreshold = n
			}
		case "fields":
			f, e := validateFields(v)
			if e != nil {
				return c, e
			}
			c.Fields = f
		default:
			return c, fault(422, "UNKNOWN_FIELD")
		}
	}
	err = s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if _, e := spaceIn(tx, tenant, space, true); e != nil {
			return e
		}
		u := stamp()
		u["embedding_model_id"] = c.EmbeddingModelID
		u["rerank_model_id"] = c.RerankModelID
		u["top_k"] = c.TopK
		u["vector_weight"] = c.VectorWeight
		u["similarity_threshold"] = c.SimilarityThreshold
		u["fields"] = c.Fields
		u["revision"] = c.Revision + 1
		r := tx.Model(&Config{}).Where("id = ? AND revision = ?", c.ID, c.Revision).Updates(u)
		if r.Error != nil {
			return r.Error
		}
		if r.RowsAffected != 1 {
			return fault(409, "REVISION_CONFLICT")
		}
		return bump(tx, space)
	})
	c.Revision++
	return c, err
}
func requestHash(v any) string {
	raw, _ := json.Marshal(v)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
func validateKey(key string) error {
	if len(key) < 1 || len(key) > 128 {
		return fault(422, "IDEMPOTENCY_KEY_REQUIRED")
	}
	for _, b := range []byte(key) {
		if b < 33 || b > 126 {
			return fault(422, "INVALID_IDEMPOTENCY_KEY")
		}
	}
	return nil
}
func existingOperation(db *gorm.DB, tenant, kind, key, hash string) (*Operation, error) {
	if e := db.Exec("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", "skills-request:"+tenant+":"+kind+":"+key).Error; e != nil {
		return nil, e
	}
	var o Operation
	err := db.Where("tenant_id = ? AND kind = ? AND (idempotency_key = ? OR jsonb_exists(payload->'idempotency_aliases', ?))", tenant, kind, key, key).First(&o).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if o.BackendOwner != "go" {
		return nil, fault(409, "BACKEND_OWNER_MISMATCH")
	}
	expectedHash := o.RequestHash
	if o.IdempotencyKey != key {
		expectedHash = stringValue(asJSON(o.Payload["idempotency_aliases"])[key])
	}
	if expectedHash != hash {
		return nil, fault(409, "IDEMPOTENCY_CONFLICT")
	}
	return &o, nil
}
func makeOperation(tenant, kind, key string, payload JSON) Operation {
	return Operation{ID: newID(), TenantID: tenant, BackendOwner: "go", Kind: kind, State: "pending", Phase: "sealed", IdempotencyKey: key, RequestHash: requestHash(payload), Payload: payload, Progress: JSON{"completed": 0, "total": 1}, Result: JSON{"items": []any{}}, Revision: 1, Times: nowTimes()}
}
func (s *Service) Operation(ctx context.Context, tenant, id string) (Operation, error) {
	var o Operation
	e := s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", id, tenant).First(&o).Error
	return o, missing(e)
}
func (s *Service) Retry(ctx context.Context, tenant, id string) (Operation, error) {
	o, e := s.Operation(ctx, tenant, id)
	if e != nil {
		return o, e
	}
	if o.BackendOwner != "go" {
		return o, fault(409, "BACKEND_OWNER_MISMATCH")
	}
	if o.State != "failed" && o.State != "partial" {
		return o, fault(409, "OPERATION_NOT_FAILED")
	}
	if o.Phase == "staging" || o.Payload["incomplete_upload"] == true && o.Error["error_code"] == "INCOMPLETE_UPLOAD" {
		return o, fault(409, "INCOMPLETE_UPLOAD")
	}
	u := stamp()
	u["state"] = "pending"
	u["error"] = nil
	u["lease_owner"] = nil
	u["lease_expires_at"] = nil
	u["revision"] = gorm.Expr("revision + 1")
	result := s.DB.WithContext(ctx).Table(Operation{}.TableName()).Where("id = ? AND revision = ? AND state IN ?", id, o.Revision, []string{"failed", "partial"}).Updates(u)
	if result.Error != nil {
		return o, result.Error
	}
	if result.RowsAffected != 1 {
		return o, fault(409, "REVISION_CONFLICT")
	}
	return s.Operation(ctx, tenant, id)
}
func (s *Service) Enqueue(ctx context.Context, tenant, space, resource, kind, key string, payload JSON) (Operation, error) {
	if e := validateKey(key); e != nil {
		return Operation{}, e
	}
	payload["space_id"] = space
	payload["resource_id"] = resource
	o := makeOperation(tenant, kind, key, payload)
	if space != "" {
		o.SpaceID = &space
	}
	if resource != "" {
		o.ResourceID = &resource
	}
	err := s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		old, e := existingOperation(tx, tenant, kind, key, o.RequestHash)
		if e != nil {
			return e
		}
		if old != nil {
			o = *old
			return nil
		}
		sp, e := spaceIn(tx.Clauses(clause.Locking{Strength: "UPDATE"}), tenant, space, true)
		if e != nil {
			return e
		}
		_ = sp
		switch kind {
		case "reindex":
		case "activate":
			var skill Skill
			if e = tx.Where("id = ? AND tenant_id = ? AND space_id = ? AND state = 'active'", resource, tenant, space).First(&skill).Error; e != nil {
				return missing(e)
			}
			r, ok := number(payload["revision"])
			if !ok || int64(r) != skill.Revision {
				return fault(409, "REVISION_CONFLICT")
			}
			if v := stringValue(payload["version_id"]); v != "" {
				var count int64
				tx.Model(&Version{}).Where("id = ? AND skill_id = ? AND tenant_id = ? AND state = 'installed'", v, resource, tenant).Count(&count)
				if count != 1 {
					return fault(404, "NOT_FOUND")
				}
			}
		case "delete_version":
			var v Version
			e = tx.Joins("JOIN t_ai_skills s ON s.id = t_ai_skill_versions.skill_id").Where("t_ai_skill_versions.id = ? AND t_ai_skill_versions.tenant_id = ? AND s.space_id = ? AND t_ai_skill_versions.state = 'installed'", resource, tenant, space).First(&v).Error
			if e != nil {
				return missing(e)
			}
			var count int64
			tx.Model(&Skill{}).Where("id = ? AND active_version_id = ?", v.SkillID, v.ID).Count(&count)
			if count > 0 {
				return fault(409, "ACTIVE_VERSION")
			}
			if e = tx.Model(&Version{}).Where("id = ?", resource).Update("state", "deleting").Error; e != nil {
				return e
			}
			if e = bump(tx, space); e != nil {
				return e
			}
		case "delete_skill":
			r := tx.Model(&Skill{}).Where("id = ? AND tenant_id = ? AND space_id = ? AND state = 'active'", resource, tenant, space).Updates(map[string]any{"state": "deleting", "active_version_id": nil, "revision": gorm.Expr("revision + 1")})
			if r.Error != nil {
				return r.Error
			}
			if r.RowsAffected != 1 {
				return fault(404, "NOT_FOUND")
			}
			if e = bump(tx, space); e != nil {
				return e
			}
		case "delete_space":
			if e = tx.Model(&Space{}).Where("id = ?", space).Updates(map[string]any{"state": "deleting", "revision": gorm.Expr("revision + 1")}).Error; e != nil {
				return e
			}
		default:
			return fault(422, "INVALID_OPERATION")
		}
		return tx.Create(&o).Error
	})
	return o, err
}
func stringValue(v any) string { str, _ := v.(string); return str }
