package service

import (
	"bytes"
	"context"
	"errors"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	redisengine "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/storage"
)

var (
	ErrLinkArgument    = errors.New("provide file_ids, kb_ids and mode replace or add")
	ErrLinkUnavailable = errors.New("source file or dataset is unavailable or unauthorized")
	ErrLinkChanged     = errors.New("source file or references changed; retry conversion")
)

type LinkToDatasetsRequest struct {
	FileIDs []string `json:"file_ids"`
	KBIDs   []string `json:"kb_ids"`
}

// File2DocumentService completes conversion before acknowledging it. Inserts are
// transactional; retirement uses the existing durable cancellation/cleanup protocol.
type File2DocumentService struct {
	db       *gorm.DB
	objects  storage.Storage
	deletion fileDocumentRetirer
}

type fileDocumentRetirer interface {
	DeleteDocuments(context.Context, string, string, []string, bool) (int, error)
}

func NewFile2DocumentService() *File2DocumentService { return &File2DocumentService{db: dao.DB} }

func linkIDs(ids []string) ([]string, error) {
	if ids == nil {
		return nil, ErrLinkArgument
	}
	result := make([]string, 0, len(ids))
	seen := map[string]bool{}
	for _, id := range ids {
		if id == "" || strings.TrimSpace(id) != id || len(id) > 256 || strings.ContainsAny(id, "/\\\x00") {
			return nil, ErrLinkArgument
		}
		if !seen[id] {
			result = append(result, id)
			seen[id] = true
		}
	}
	sort.Strings(result)
	return result, nil
}

func authorizeLinkTenant(tx *gorm.DB, actor, tenant string) error {
	var user entity.User
	if err := tx.Clauses(clause.Locking{Strength: "SHARE"}).Where("id=? AND status='1' AND is_active=true", actor).Take(&user).Error; err != nil {
		return linkAccessError(err)
	}
	var owner entity.Tenant
	if err := tx.Clauses(clause.Locking{Strength: "SHARE"}).Where("id=? AND status='1'", tenant).Take(&owner).Error; err != nil {
		return linkAccessError(err)
	}
	if actor == tenant {
		return nil
	}
	var membership entity.UserTenant
	return linkAccessError(tx.Clauses(clause.Locking{Strength: "SHARE"}).Where("tenant_id=? AND user_id=? AND status='1' AND role IN ?", tenant, actor, []string{"owner", "admin", "normal"}).Take(&membership).Error)
}

func linkAccessError(err error) error {
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return ErrLinkUnavailable
	}
	return err
}

func authorizeLinkKB(tx *gorm.DB, actor string, kb *entity.Knowledgebase) error {
	if kb.Status == nil || *kb.Status != "1" || (kb.TenantID != actor && kb.Permission != "team") {
		return ErrLinkUnavailable
	}
	return authorizeLinkTenant(tx, actor, kb.TenantID)
}

func authorizeLinkFile(tx *gorm.DB, actor string, file *entity.File) error {
	if file.SourceType != "" && file.SourceType != "local" && file.SourceType != "knowledgebase" {
		return ErrLinkUnavailable
	}
	if err := authorizeLinkTenant(tx, actor, file.TenantID); err != nil {
		return err
	}
	if actor == file.TenantID {
		return nil
	}
	var kbs []entity.Knowledgebase
	if err := tx.Model(&entity.Knowledgebase{}).Joins("JOIN t_ai_documents d ON d.kb_id=t_ai_knowledgebases.id JOIN t_ai_file2documents f ON f.document_id=d.id").Where("f.file_id=?", file.ID).Find(&kbs).Error; err != nil {
		return err
	}
	for i := range kbs {
		if err := authorizeLinkKB(tx, actor, &kbs[i]); err == nil {
			return nil
		} else if !errors.Is(err, ErrLinkUnavailable) {
			return err
		}
	}
	return ErrLinkUnavailable
}

// collectLinkFiles checks every requested folder and every descendant before any
// mutation. Ancestry is validated independently of a leaf's dataset share grant.
func collectLinkFiles(tx *gorm.DB, actor string, ids []string) (map[string]entity.File, []string, error) {
	files := map[string]entity.File{}
	leaves := map[string]bool{}
	visiting := map[string]bool{}
	visited := map[string]bool{}
	var load func(string) (entity.File, error)
	load = func(id string) (entity.File, error) {
		if f, ok := files[id]; ok {
			return f, nil
		}
		var f entity.File
		err := tx.Where("id=?", id).Take(&f).Error
		if err != nil {
			return f, linkAccessError(err)
		}
		files[id] = f
		return f, nil
	}
	var walk func(string) error
	walk = func(id string) error {
		if visiting[id] {
			return ErrLinkUnavailable
		}
		if visited[id] {
			return nil
		}
		visiting[id] = true
		defer delete(visiting, id)
		file, err := load(id)
		if err != nil {
			return err
		}
		if err = authorizeLinkFile(tx, actor, &file); err != nil {
			return err
		}
		ancestor := file
		seen := map[string]bool{}
		for ancestor.ParentID != ancestor.ID {
			if seen[ancestor.ID] || ancestor.ParentID == "" {
				return ErrLinkUnavailable
			}
			seen[ancestor.ID] = true
			parent, e := load(ancestor.ParentID)
			if e != nil {
				return e
			}
			if parent.Type != FileTypeFolder || parent.TenantID != file.TenantID || (parent.SourceType != "" && parent.SourceType != "local" && parent.SourceType != "knowledgebase") {
				return ErrLinkUnavailable
			}
			ancestor = parent
		}
		if file.Type == FileTypeFolder {
			var children []entity.File
			if err = tx.Where("parent_id=? AND id<>?", id, id).Order("id").Find(&children).Error; err != nil {
				return err
			}
			for _, child := range children {
				if err = walk(child.ID); err != nil {
					return err
				}
			}
		} else {
			leaves[id] = true
		}
		visited[id] = true
		return nil
	}
	for _, id := range ids {
		if err := walk(id); err != nil {
			return nil, nil, err
		}
	}
	result := make([]string, 0, len(leaves))
	for id := range leaves {
		result = append(result, id)
	}
	sort.Strings(result)
	return files, result, nil
}

func fileLinkParser(file entity.File, kb entity.Knowledgebase) string {
	if file.Type == "visual" {
		return "picture"
	}
	if file.Type == "aural" {
		return "audio"
	}
	switch strings.ToLower(filepath.Ext(file.Name)) {
	case ".ppt", ".pptx", ".pages":
		return "presentation"
	case ".msg", ".eml":
		return "email"
	}
	return kb.ParserID
}

type linkObject struct{ bucket, key string }

func copyLinkSource(ctx context.Context, store storage.Storage, bucket, key string, data []byte, created *[]linkObject) error {
	checker, ok := store.(agentUploadObjectChecker)
	if !ok {
		return errors.New("conversion object verification unavailable")
	}
	if _, ok = store.(documentContextRemover); !ok {
		return errors.New("conversion object compensation unavailable")
	}
	if !safeStorageAddress(bucket, key) {
		return ErrLinkUnavailable
	}
	exists, err := checker.ObjectExistsContext(ctx, bucket, key)
	if err != nil {
		return err
	}
	if exists {
		return ErrLinkChanged
	}
	*created = append(*created, linkObject{bucket, key})
	if err = store.Put(bucket, key, data); err != nil {
		return err
	}
	reader, ok := store.(storage.ContextReader)
	if !ok {
		return errors.New("conversion object reads cannot be canceled")
	}
	read, err := reader.GetContext(ctx, bucket, key)
	if err != nil || !bytes.Equal(read, data) {
		return errors.New("converted source bytes could not be verified")
	}
	return ctx.Err()
}

func (s *File2DocumentService) linkToDatasets(ctx context.Context, actor string, req LinkToDatasetsRequest, mode string, expectedLeaves []string) (resultErr error) {
	fileIDs, err := linkIDs(req.FileIDs)
	if err != nil || len(fileIDs) == 0 {
		return ErrLinkArgument
	}
	targets, err := linkIDs(req.KBIDs)
	if err != nil || (mode != "replace" && mode != "add") {
		return ErrLinkArgument
	}
	if actor == "" {
		return ErrLinkUnavailable
	}
	if s == nil || s.db == nil {
		return errors.New("file conversion SQL unavailable")
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	store := s.objects
	if store == nil {
		store = storage.GetStorageFactory().GetStorage()
	}
	var created []linkObject
	committed := false
	defer func() {
		if committed || len(created) == 0 {
			return
		}
		cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer cancel()
		checker, ok := store.(agentUploadObjectChecker)
		remover, canRemove := store.(documentContextRemover)
		if !ok || !canRemove {
			resultErr = errors.Join(resultErr, errors.New("conversion object cleanup unavailable"))
			return
		}
		for _, object := range created {
			removeErr := remover.RemoveContext(cleanupCtx, object.bucket, object.key)
			present, readErr := checker.ObjectExistsContext(cleanupCtx, object.bucket, object.key)
			if removeErr != nil || readErr != nil || present {
				resultErr = errors.Join(resultErr, errors.New("conversion object cleanup could not be confirmed"))
			}
		}
	}()
	var removals []entity.Document
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		files, leaves, err := collectLinkFiles(tx, actor, fileIDs)
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(leaves, expectedLeaves) {
			return ErrLinkChanged
		}
		var links []entity.File2Document
		if len(leaves) > 0 {
			if err = tx.Where("file_id IN ?", leaves).Order("id").Find(&links).Error; err != nil {
				return err
			}
		}
		docIDs := []string{}
		for _, link := range links {
			if link.DocumentID == nil || link.FileID == nil {
				return ErrLinkChanged
			}
			docIDs = append(docIDs, *link.DocumentID)
		}
		sort.Strings(docIDs)
		docs := map[string]entity.Document{}
		kbIDs := append([]string(nil), targets...)
		for _, id := range docIDs {
			var doc entity.Document
			if err = tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id=?", id).Take(&doc).Error; err != nil {
				return linkAccessError(err)
			}
			var count int64
			if err = tx.Model(&entity.File2Document{}).Where("document_id=?", id).Count(&count).Error; err != nil {
				return err
			}
			if count != 1 {
				return ErrLinkUnavailable
			}
			docs[id] = doc
			kbIDs = append(kbIDs, doc.KbID)
		}
		sort.Strings(kbIDs)
		kbs := map[string]entity.Knowledgebase{}
		for _, id := range kbIDs {
			if _, ok := kbs[id]; ok {
				continue
			}
			var kb entity.Knowledgebase
			if err = tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id=?", id).Take(&kb).Error; err != nil {
				return linkAccessError(err)
			}
			if err = authorizeLinkKB(tx, actor, &kb); err != nil {
				return err
			}
			kbs[id] = kb
		}
		allIDs := make([]string, 0, len(files))
		for id := range files {
			allIDs = append(allIDs, id)
		}
		sort.Strings(allIDs)
		for _, id := range allIDs {
			var fresh entity.File
			if err = tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id=?", id).Take(&fresh).Error; err != nil {
				return linkAccessError(err)
			}
			if !reflect.DeepEqual(fresh, files[id]) {
				return ErrLinkChanged
			}
		}
		// Existing deletion intentionally requires every document's source file
		// to belong to its dataset tenant. Do not create undeletable cross-tenant
		// registrations, even when the actor has access to both resources.
		for _, id := range leaves {
			for _, kbID := range targets {
				if files[id].TenantID != kbs[kbID].TenantID {
					return ErrLinkUnavailable
				}
			}
		}
		var freshLinks []entity.File2Document
		if len(leaves) > 0 {
			if err = tx.Where("file_id IN ?", leaves).Order("id").Find(&freshLinks).Error; err != nil {
				return err
			}
		}
		if len(links) != len(freshLinks) || (len(links) > 0 && !reflect.DeepEqual(links, freshLinks)) {
			return ErrLinkChanged
		}
		// Repeat expansion under parent locks; files moved into/out of a selected
		// folder cannot escape the permission snapshot or silently disappear.
		freshFiles, freshLeaves, err := collectLinkFiles(tx, actor, fileIDs)
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(files, freshFiles) || !reflect.DeepEqual(leaves, freshLeaves) {
			return ErrLinkChanged
		}
		selected := map[string]bool{}
		for _, id := range targets {
			selected[id] = true
		}
		for _, doc := range docs {
			if mode == "replace" && !selected[doc.KbID] {
				if _, err = authorizeDocumentDelete(tx, doc.KbID, actor, false); err != nil {
					return err
				}
				removals = append(removals, doc)
			}
		}
		for _, id := range leaves {
			file := files[id]
			existing := map[string]bool{}
			var origin *entity.Document
			for _, link := range links {
				if *link.FileID == id {
					doc := docs[*link.DocumentID]
					existing[doc.KbID] = true
					if origin == nil {
						copy := doc
						origin = &copy
					}
				}
			}
			// Clearing all links must leave a readable source file. Materialize
			// dataset-sourced bytes in the already-authorized parent namespace
			// before retiring the last document that names the original bucket.
			if len(targets) == 0 && mode == "replace" && file.SourceType == "knowledgebase" {
				if origin == nil {
					return ErrLinkUnavailable
				}
				source, err := (&DocumentFileService{db: tx, store: store}).readDocument(ctx, origin)
				if err != nil {
					return err
				}
				location := common.GenerateUUID()
				if err = copyLinkSource(ctx, store, file.ParentID, location, source.Data, &created); err != nil {
					return err
				}
				if err = tx.Model(&entity.File{}).Where("id=?", id).Updates(map[string]any{"source_type": "local", "location": location, "size": len(source.Data)}).Error; err != nil {
					return err
				}
			}
			for _, kbID := range targets {
				if existing[kbID] {
					continue
				}
				kb := kbs[kbID]
				docID := common.GenerateUUID()
				location := ""
				if file.Location != nil {
					location = *file.Location
				}
				if file.SourceType == "knowledgebase" {
					if origin == nil {
						return ErrLinkUnavailable
					}
					source, err := (&DocumentFileService{db: tx, store: store}).readDocument(ctx, origin)
					if err != nil {
						return err
					}
					location = docID
					if err = copyLinkSource(ctx, store, kbID, location, source.Data, &created); err != nil {
						return err
					}
				} else {
					if _, ok := store.(storage.ContextReader); !ok {
						return errors.New("conversion object reads cannot be canceled")
					}
					if !safeStorageAddress(file.ParentID, location) {
						return ErrLinkUnavailable
					}
					data, err := (&DocumentFileService{store: store}).readObject(ctx, file.ParentID, location)
					if err != nil {
						return err
					}
					if int64(len(data)) != file.Size {
						return errors.New("source bytes do not match file size")
					}
				}
				name := file.Name
				suffix := strings.TrimPrefix(filepath.Ext(name), ".")
				parserConfig := kb.ParserConfig
				if parserConfig == nil {
					parserConfig = entity.JSONMap{}
				}
				status, run := "1", "0"
				doc := entity.Document{ID: docID, KbID: kbID, ParserID: fileLinkParser(file, kb), ParserConfig: parserConfig, PipelineID: kb.PipelineID, CreatedBy: actor, Type: file.Type, Name: &name, Suffix: suffix, Location: &location, Size: file.Size, Status: &status, Run: &run, SourceType: "local"}
				if err = tx.Create(&doc).Error; err != nil {
					return err
				}
				fileID := id
				if err = dao.NewFile2DocumentDAO().CreateInTransaction(tx, &entity.File2Document{ID: common.GenerateUUID(), FileID: &fileID, DocumentID: &doc.ID}); err != nil {
					return err
				}
				result := tx.Model(&entity.Knowledgebase{}).Where("id=?", kbID).Update("doc_num", gorm.Expr("doc_num + 1"))
				if result.Error != nil {
					return result.Error
				}
				if result.RowsAffected != 1 {
					return ErrLinkChanged
				}
			}
		}
		return ctx.Err()
	})
	if err != nil {
		return err
	}
	committed = true
	sort.Slice(removals, func(i, j int) bool { return removals[i].ID < removals[j].ID })
	deletion := s.deletion
	if deletion == nil {
		var redisClient = redisengine.Get()
		if redisClient == nil && len(removals) > 0 {
			return errors.New("conversion retirement cache unavailable")
		}
		if redisClient != nil {
			deletion = NewDocumentDeleteService(s.db, redisClient.GetClient(), engine.Get(), store)
		}
	}
	for _, doc := range removals {
		if deletion == nil {
			return errors.New("conversion retirement unavailable")
		}
		if _, err = deletion.DeleteDocuments(ctx, doc.KbID, actor, []string{doc.ID}, false); err != nil {
			return err
		}
	}
	return nil
}
