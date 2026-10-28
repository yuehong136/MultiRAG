package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"math/big"
	"reflect"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/storage"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

var (
	ErrDocumentDeleteForbidden      = errors.New("dataset unavailable or no authorization to delete documents")
	ErrInvalidDocumentDeleteRequest = errors.New("provide document ids or delete_all, exclusively")
	ErrDocumentDeleteRetryable      = errors.New("document cleanup incomplete; canceled documents remain available for retry")
	ErrDocumentDeleteCommitted      = errors.New("documents deleted; graph writer lease cleanup could not be confirmed")
)

const documentDeletePageSize = 256

// DocumentDeleteService keeps SQL parents until every external deletion has
// acknowledged success. Cancellation is a separate durable phase: a failed
// blob/index operation must never revive a producer whose output was removed.
type DocumentDeleteService struct {
	db         *gorm.DB
	redis      *redis.Client
	index      engine.DocEngine
	objects    storage.Storage
	engineType server.EngineType
}

func NewDocumentDeleteService(db *gorm.DB, redisClient *redis.Client, index engine.DocEngine, objects storage.Storage, engineTypes ...server.EngineType) *DocumentDeleteService {
	var engineType server.EngineType
	if len(engineTypes) > 0 {
		engineType = engineTypes[0]
	} else if cfg := server.GetConfig(); cfg != nil {
		engineType = cfg.DocEngine.Type
	}
	return &DocumentDeleteService{db: db, redis: redisClient, index: index, objects: objects, engineType: engineType}
}

type documentDeleteFailure struct {
	stage string
	cause error
}

func (e *documentDeleteFailure) Error() string {
	return ErrDocumentDeleteRetryable.Error() + ": " + e.stage
}
func (e *documentDeleteFailure) Unwrap() []error { return []error{ErrDocumentDeleteRetryable, e.cause} }

func documentDeleteError(stage string, cause error) error {
	return &documentDeleteFailure{stage: stage, cause: cause}
}

type documentDeleteSnapshot struct {
	documents []entity.Document
	tasks     []entity.Task
	tenantID  string
	recovery  []documentDeleteRecovery
}

type documentDeleteRecovery struct{ ID, DocumentID, TaskID, Wire string }

type documentDeleteFiles struct {
	links   []entity.File2Document
	files   []entity.File
	orphans []entity.File
}

type documentCancelReceipt struct {
	taskID          string
	token           string
	previousBinding string
}

type documentDeleteObject struct{ bucket, key string }

type documentContextRemover interface {
	RemoveContext(context.Context, string, string, ...string) error
}

func normalizedDocumentDeleteIDs(ids []string, deleteAll bool) ([]string, error) {
	if deleteAll {
		if ids != nil {
			return nil, ErrInvalidDocumentDeleteRequest
		}
		return nil, nil
	}
	if ids == nil {
		return nil, ErrInvalidDocumentDeleteRequest
	}
	result := make([]string, 0, len(ids))
	seen := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		if strings.TrimSpace(id) != id || id == "" || len(id) > 256 || strings.ContainsFunc(id, unicode.IsControl) {
			return nil, ErrInvalidDocumentDeleteRequest
		}
		if _, exists := seen[id]; !exists {
			result = append(result, id)
			seen[id] = struct{}{}
		}
	}
	sort.Strings(result)
	return result, nil
}

// DeleteDocuments returns either the complete distinct count or zero and an
// error. An explicit [] is a no-op, after authorization; it never means all.
func (s *DocumentDeleteService) DeleteDocuments(ctx context.Context, datasetID, userID string, ids []string, deleteAll bool) (int, error) {
	selected, err := normalizedDocumentDeleteIDs(ids, deleteAll)
	if err != nil {
		return 0, err
	}
	if datasetID == "" || userID == "" {
		return 0, ErrDocumentDeleteForbidden
	}
	if s == nil || s.db == nil {
		return 0, documentDeleteError("SQL unavailable", nil)
	}
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	if _, err := authorizeDocumentDelete(s.db.WithContext(ctx), datasetID, userID, false); err != nil {
		return 0, err
	}
	if deleteAll {
		selected, err = s.documentIDs(ctx, datasetID)
		if err != nil {
			return 0, documentDeleteError("document enumeration failed", err)
		}
	}
	if len(selected) == 0 {
		return 0, nil
	}
	kb, err := authorizeDocumentDelete(s.db.WithContext(ctx), datasetID, userID, false)
	if err != nil {
		return 0, err
	}
	base, err := s.documentDeleteBase(kb)
	if err != nil {
		return 0, documentDeleteError("index scope unavailable", err)
	}
	if s.index == nil {
		return 0, documentDeleteError("document index unavailable", nil)
	}
	// Probe the actual backend before cancellation or object deletion. A zero
	// SQL chunk counter never proves that the external store has no rows.
	if _, err := s.index.ChunkStoreExists(ctx, base, kb.ID); err != nil {
		return 0, documentDeleteError("chunk store probe failed", err)
	}
	if _, err := s.index.MetadataStoreExists(ctx, kb.TenantID); err != nil {
		return 0, documentDeleteError("metadata store probe failed", err)
	}
	snapshot, err := s.cancelDocuments(ctx, datasetID, userID, selected)
	if err != nil {
		return 0, err
	}
	// Infinity's SDK does not consume context. Keep the SQL ownership locks
	// until accepted native work actually returns, even after HTTP disconnect.
	if err := s.finishDocumentDelete(context.WithoutCancel(ctx), datasetID, userID, selected, snapshot); err != nil {
		if errors.Is(err, ErrDocumentDeleteCommitted) {
			return len(selected), err
		}
		return 0, err
	}
	return len(selected), nil
}

func authorizeDocumentDelete(tx *gorm.DB, datasetID, userID string, lock bool) (*entity.Knowledgebase, error) {
	query := tx.Where("id = ? AND status = ?", datasetID, "1")
	if lock {
		query = query.Clauses(clause.Locking{Strength: "UPDATE"})
	}
	var kb entity.Knowledgebase
	if err := query.Take(&kb).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrDocumentDeleteForbidden
		}
		return nil, documentDeleteError("dataset authorization unavailable", err)
	}
	if kb.TenantID == userID {
		return &kb, nil
	}
	var memberships []entity.UserTenant
	members := tx.Where("tenant_id = ? AND user_id = ? AND status = ? AND role IN ?", kb.TenantID, userID, "1", []string{"owner", "admin"})
	if lock {
		members = members.Clauses(clause.Locking{Strength: "SHARE"})
	}
	if err := members.Find(&memberships).Error; err != nil {
		return nil, documentDeleteError("membership unavailable", err)
	}
	if len(memberships) != 1 {
		return nil, ErrDocumentDeleteForbidden
	}
	return &kb, nil
}

func (s *DocumentDeleteService) documentIDs(ctx context.Context, datasetID string) ([]string, error) {
	ids := []string{}
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		last := ""
		for {
			page := []string{}
			query := tx.Model(&entity.Document{}).Where("kb_id = ?", datasetID)
			if last != "" {
				query = query.Where("id > ?", last)
			}
			if err := query.Order("id").Limit(documentDeletePageSize).Pluck("id", &page).Error; err != nil {
				return err
			}
			if len(page) == 0 {
				return nil
			}
			ids = append(ids, page...)
			last = page[len(page)-1]
		}
	}, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	return ids, err
}

func lockedDeleteDocuments(tx *gorm.DB, datasetID string, ids []string) ([]entity.Document, []entity.Task, error) {
	documents := make([]entity.Document, 0, len(ids))
	// Every participant, including Python producers, locks Doc before Task.
	for start := 0; start < len(ids); start += documentDeletePageSize {
		end := min(start+documentDeletePageSize, len(ids))
		var page []entity.Document
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id IN ? AND kb_id = ?", ids[start:end], datasetID).Order("id").Find(&page).Error; err != nil {
			return nil, nil, err
		}
		if len(page) != end-start {
			return nil, nil, ErrDocumentDeleteForbidden
		}
		documents = append(documents, page...)
	}
	tasks := []entity.Task{}
	for start := 0; start < len(ids); start += documentDeletePageSize {
		end := min(start+documentDeletePageSize, len(ids))
		var page []entity.Task
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("doc_id IN ?", ids[start:end]).Order("doc_id, id").Find(&page).Error; err != nil {
			return nil, nil, err
		}
		tasks = append(tasks, page...)
	}
	return documents, tasks, nil
}

func (s *DocumentDeleteService) cancelDocuments(ctx context.Context, datasetID, userID string, ids []string) (*documentDeleteSnapshot, error) {
	snapshot := &documentDeleteSnapshot{}
	receipts := []documentCancelReceipt{}
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		documents, tasks, err := lockedDeleteDocuments(tx, datasetID, ids)
		if err != nil {
			return err
		}
		kb, err := authorizeDocumentDelete(tx, datasetID, userID, false)
		if err != nil {
			return err
		}
		for _, doc := range documents {
			if doc.Location != nil && *doc.Location != "" && !validDocumentDeleteObject(documentDeleteObject{doc.KbID, *doc.Location}) {
				return errors.New("document source address is invalid")
			}
			if doc.Thumbnail != nil && *doc.Thumbnail != "" && !strings.HasPrefix(*doc.Thumbnail, "data:") && !validDocumentDeleteObject(documentDeleteObject{doc.KbID, *doc.Thumbnail}) {
				return errors.New("document thumbnail address is invalid")
			}
		}
		recovery, err := s.resolvedDocumentSourceRecovery(ctx, tx, ids, tasks)
		if err != nil {
			return err
		}
		kb, err = authorizeDocumentDelete(tx, datasetID, userID, true)
		if err != nil {
			return err
		}
		// Shared file writers take their dataset lock before any File lock.
		// Validate the complete source footprint before the first cancellation.
		if _, err := readDocumentDeleteFiles(tx, kb.TenantID, ids, true); err != nil {
			return err
		}
		byID := make(map[string]entity.Document, len(documents))
		for _, doc := range documents {
			byID[doc.ID] = doc
		}
		if len(tasks) > 0 && s.redis == nil {
			return documentDeleteError("task cancellation unavailable", nil)
		}
		// Validate every trusted runtime binding before the first Redis write.
		bindings := make(map[string]string, len(tasks))
		for _, task := range tasks {
			raw, binding, err := (&TaskService{redis: s.redis}).readBinding(ctx, task.ID)
			if err != nil {
				return documentDeleteError("task runtime unavailable", err)
			}
			if binding != nil {
				doc := byID[task.DocID]
				if binding.TenantID != kb.TenantID || binding.ResourceID != doc.ID || binding.PrincipalID != doc.CreatedBy {
					return documentDeleteError("task runtime does not belong to document", nil)
				}
				bindings[task.ID] = raw
			}
		}
		for _, task := range tasks {
			receipt := documentCancelReceipt{taskID: task.ID, token: uuid.NewString(), previousBinding: bindings[task.ID]}
			// Retain the token even if the acknowledgement is lost; rollback can
			// inspect/CAS only this attempt without erasing another cancellation.
			receipts = append(receipts, receipt)
			if receipt.previousBinding != "" {
				var binding taskBinding
				if err := json.Unmarshal([]byte(receipt.previousBinding), &binding); err != nil {
					return err
				}
				if binding.State == "active" {
					written, err := s.redis.Eval(ctx, cancelRuntimeScript, []string{taskRuntimePrefix + task.ID, task.ID + "-cancel"}, receipt.previousBinding, receipt.token, int(taskRuntimeTTL.Seconds())).Int()
					if err != nil || written != 1 {
						return documentDeleteError("task cancellation changed; retry", err)
					}
				}
			} else {
				written, err := s.redis.SetNX(ctx, task.ID+"-cancel", receipt.token, taskRuntimeTTL).Result()
				if err != nil {
					return documentDeleteError("task cancellation failed", err)
				}
				if !written {
					value, err := s.redis.Get(ctx, task.ID+"-cancel").Result()
					if err != nil || value == "" {
						return documentDeleteError("existing cancellation unavailable", err)
					}
				}
			}
			message := ""
			if task.ProgressMsg != nil {
				message = *task.ProgressMsg
			}
			if !strings.Contains(message, taskCancelMarker) {
				message += "\n" + taskCancelMarker + " Document deletion requested."
			}
			if err := tx.Model(&entity.Task{}).Where("id = ? AND doc_id = ?", task.ID, task.DocID).Updates(map[string]any{"progress": -1, "progress_msg": message}).Error; err != nil {
				return err
			}
		}
		for start := 0; start < len(ids); start += documentDeletePageSize {
			end := min(start+documentDeletePageSize, len(ids))
			if err := tx.Model(&entity.Document{}).Where("id IN ? AND kb_id = ?", ids[start:end], datasetID).Updates(map[string]any{"run": "2", "progress": 0}).Error; err != nil {
				return err
			}
		}
		snapshot.documents, snapshot.tasks, snapshot.tenantID, snapshot.recovery = documents, tasks, kb.TenantID, recovery
		return nil
	})
	if err != nil {
		if cleanupErr := s.compensateDocumentCancellation(receipts); cleanupErr != nil {
			return nil, documentDeleteError("SQL cancellation failed and Redis compensation is uncertain", errors.Join(err, cleanupErr))
		}
		if errors.Is(err, ErrDocumentDeleteForbidden) {
			return nil, err
		}
		return nil, documentDeleteError("cancellation transaction failed", err)
	}
	return snapshot, nil
}

func (s *DocumentDeleteService) compensateDocumentCancellation(receipts []documentCancelReceipt) error {
	if len(receipts) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for i := len(receipts) - 1; i >= 0; i-- {
		r := receipts[i]
		var err error
		if r.previousBinding != "" {
			_, err = s.redis.Eval(ctx, restoreRuntimeScript, []string{taskRuntimePrefix + r.taskID, r.taskID + "-cancel"}, r.previousBinding, r.token, int(taskRuntimeTTL.Seconds())).Result()
		} else {
			_, err = s.redis.Eval(ctx, deleteCancelScript, []string{r.taskID + "-cancel"}, r.token).Result()
		}
		if err != nil {
			return err
		}
		value, err := s.redis.Get(ctx, r.taskID+"-cancel").Result()
		if err != nil && !errors.Is(err, redis.Nil) {
			return err
		}
		if value == r.token {
			return errors.New("own cancellation token remains")
		}
	}
	return nil
}

func sameDeleteDocuments(before, after []entity.Document) bool {
	if len(before) != len(after) {
		return false
	}
	for i, old := range before {
		current := after[i]
		if old.ID != current.ID || old.KbID != current.KbID || old.CreatedBy != current.CreatedBy || old.SourceType != current.SourceType || old.ParserID != current.ParserID || old.Size != current.Size || old.ChunkNum != current.ChunkNum || old.TokenNum != current.TokenNum || !reflect.DeepEqual(old.Location, current.Location) || !reflect.DeepEqual(old.Thumbnail, current.Thumbnail) || !reflect.DeepEqual(old.ContentHash, current.ContentHash) || current.Run == nil || *current.Run != "2" {
			return false
		}
	}
	return true
}

func sameDeleteTasks(before, after []entity.Task) bool {
	if len(before) != len(after) {
		return false
	}
	for i, old := range before {
		current := after[i]
		if old.ID != current.ID || old.DocID != current.DocID || current.Progress != -1 || current.ProgressMsg == nil || !strings.Contains(*current.ProgressMsg, taskCancelMarker) {
			return false
		}
	}
	return true
}

func (s *DocumentDeleteService) finishDocumentDelete(ctx context.Context, datasetID, userID string, ids []string, snapshot *documentDeleteSnapshot) (resultErr error) {
	lease, err := s.claimDocumentGraphLease(ctx, snapshot.tenantID, datasetID)
	if err != nil {
		return documentDeleteError("dataset graph writer is active or unavailable", err)
	}
	defer func() {
		if cleanupErr := lease.release(); cleanupErr != nil {
			if resultErr == nil {
				resultErr = fmtDocumentDeleteCommitted(cleanupErr)
			} else {
				resultErr = errors.Join(resultErr, documentDeleteError("graph writer lease cleanup uncertain", cleanupErr))
			}
		}
	}()
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		documents, tasks, err := lockedDeleteDocuments(tx, datasetID, ids)
		if err != nil {
			return err
		}
		if !sameDeleteDocuments(snapshot.documents, documents) || !sameDeleteTasks(snapshot.tasks, tasks) {
			return errors.New("document or producer changed after cancellation")
		}
		recovery, err := s.resolvedDocumentSourceRecovery(ctx, tx, ids, tasks)
		if err != nil {
			return err
		}
		if !slices.Equal(recovery, snapshot.recovery) {
			return errors.New("source recovery material changed after cancellation")
		}
		kb, err := authorizeDocumentDelete(tx, datasetID, userID, false)
		if err != nil {
			return err
		}
		if kb.TenantID != snapshot.tenantID {
			return ErrDocumentDeleteForbidden
		}
		if err := lease.check(ctx); err != nil {
			return err
		}
		// Read candidate descriptors without F2D/File locks. Image locks must
		// precede KB, and every shared file writer takes KB before File.
		candidateFiles, err := readDocumentDeleteFiles(tx, kb.TenantID, ids, false)
		if err != nil {
			return err
		}
		objects, err := s.documentDeleteObjects(ctx, tx, kb, documents, tasks, candidateFiles.orphans, ids)
		if err != nil {
			return err
		}
		// Producers use Doc -> Task -> exact image -> KB. In particular, never
		// hold the KB row while waiting on another producer's image lock.
		if err := lockDocumentDeleteObjects(tx, objects); err != nil {
			return err
		}
		lockedKB, err := authorizeDocumentDelete(tx, datasetID, userID, true)
		if err != nil {
			return err
		}
		if lockedKB.TenantID != snapshot.tenantID {
			return ErrDocumentDeleteForbidden
		}
		if lockedKB.Name != kb.Name {
			return errors.New("dataset index descriptor changed during cleanup preparation")
		}
		kb = lockedKB
		lockedFiles, err := readDocumentDeleteFiles(tx, kb.TenantID, ids, true)
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(candidateFiles, lockedFiles) {
			return errors.New("source file descriptors or shared references changed during cleanup preparation")
		}
		lockedObjects, err := s.documentDeleteObjects(ctx, tx, kb, documents, tasks, lockedFiles.orphans, ids)
		if err != nil {
			return err
		}
		if !slices.Equal(objects, lockedObjects) {
			return errors.New("document object descriptors changed during cleanup preparation")
		}
		// No further image locks may be acquired while KB/File locks are held.
		files := lockedFiles.orphans
		objects, err = s.unreferencedDocumentObjects(ctx, tx, kb, objects, ids, files)
		if err != nil {
			return err
		}
		if len(objects) > 0 {
			remover, ok := s.objects.(documentContextRemover)
			if !ok {
				return errors.New("context-aware object deletion unavailable")
			}
			for _, object := range objects {
				if err := lease.check(ctx); err != nil {
					return err
				}
				if err := remover.RemoveContext(ctx, object.bucket, object.key, kb.TenantID); err != nil && !errors.Is(err, storage.ErrNotFound) && !errors.Is(err, storage.ErrBucketNotFound) {
					return err
				}
			}
		}
		if err := s.deleteDocumentGraph(ctx, kb, ids, lease); err != nil {
			return err
		}
		if err := s.deleteDocumentIndexes(ctx, kb, ids, documents, lease); err != nil {
			return err
		}
		if err := lease.check(ctx); err != nil {
			return err
		}
		for _, record := range recovery {
			result := tx.Table("t_document_source_recovery").Where("id=? AND document_id=? AND task_id=? AND wire=?", record.ID, record.DocumentID, record.TaskID, record.Wire).Delete(&struct{}{})
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected != 1 {
				return errors.New("source recovery retirement changed")
			}
		}
		for _, task := range tasks {
			// Every byte named by these reservations has already been deleted
			// or retained because an independent reference protects it.
			if err := s.redis.Del(ctx, "document-task-images:"+task.ID).Err(); err != nil {
				return err
			}
		}
		for start := 0; start < len(ids); start += documentDeletePageSize {
			end := min(start+documentDeletePageSize, len(ids))
			batch := ids[start:end]
			if err := tx.Where("doc_id IN ?", batch).Delete(&entity.Task{}).Error; err != nil {
				return err
			}
			if err := tx.Where("document_id IN ?", batch).Delete(&entity.File2Document{}).Error; err != nil {
				return err
			}
			result := tx.Where("id IN ? AND kb_id = ?", batch, datasetID).Delete(&entity.Document{})
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected != int64(len(batch)) {
				return errors.New("document retirement count changed")
			}
		}
		for _, file := range files {
			stillLinked := tx.Model(&entity.File2Document{}).Select("1").Where("file_id = ?", file.ID)
			result := tx.Where("id = ? AND tenant_id = ? AND source_type = ? AND NOT EXISTS (?)", file.ID, kb.TenantID, string(entity.FileSourceKnowledgebase), stillLinked).Delete(&entity.File{})
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected != 1 {
				return errors.New("file acquired another reference")
			}
		}
		var chunks, tokens int64
		for _, doc := range documents {
			chunks += doc.ChunkNum
			tokens += doc.TokenNum
		}
		if err := tx.Model(&entity.Knowledgebase{}).Where("id = ? AND tenant_id = ?", datasetID, kb.TenantID).Updates(map[string]any{"doc_num": gorm.Expr("doc_num - ?", len(ids)), "chunk_num": gorm.Expr("chunk_num - ?", chunks), "token_num": gorm.Expr("token_num - ?", tokens)}).Error; err != nil {
			return err
		}
		return lease.check(ctx)
	})
	if err != nil {
		return documentDeleteError("external cleanup or final SQL transaction failed", err)
	}
	// The cancel keys remain until their bounded TTL, fencing queued messages.
	return nil
}

func (s *DocumentDeleteService) resolvedDocumentSourceRecovery(ctx context.Context, tx *gorm.DB, ids []string, tasks []entity.Task) ([]documentDeleteRecovery, error) {
	var present bool
	switch tx.Dialector.Name() {
	case "postgres":
		if err := tx.Raw("SELECT to_regclass('t_document_source_recovery') IS NOT NULL").Scan(&present).Error; err != nil {
			return nil, err
		}
	case "mysql":
		if err := tx.Raw("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='t_document_source_recovery')").Scan(&present).Error; err != nil {
			return nil, err
		}
	default:
		return nil, errors.New("source recovery authority probe unavailable")
	}
	var records []documentDeleteRecovery
	keys := map[string]bool{}
	for _, task := range tasks {
		keys["document-source-recovery:"+task.DocID+":"+task.ID] = true
	}
	if present {
		if err := tx.Table("t_document_source_recovery").Clauses(clause.Locking{Strength: "UPDATE"}).Select("id,document_id,task_id,wire").Where("document_id IN ?", ids).Order("id").Find(&records).Error; err != nil {
			return nil, err
		}
		for _, record := range records {
			if err := validateDocumentSourceRecovery(record); err != nil {
				return nil, err
			}
			keys["document-source-recovery:"+record.DocumentID+":"+record.TaskID] = true
		}
	}
	if len(keys) > 0 && s.redis == nil {
		return nil, errors.New("source recovery mirrors unavailable")
	}
	for key := range keys {
		count, err := s.redis.Exists(ctx, key).Result()
		if err != nil {
			return nil, err
		}
		if count != 0 {
			return nil, errors.New("source recovery mirror requires reconciliation before deletion")
		}
	}
	return records, nil
}

func readDocumentDeleteFiles(tx *gorm.DB, tenantID string, ids []string, lock bool) (*documentDeleteFiles, error) {
	var links []entity.File2Document
	linkQuery := tx.Where("document_id IN ?", ids).Order("id")
	if lock {
		linkQuery = linkQuery.Clauses(clause.Locking{Strength: "UPDATE"})
	}
	if err := linkQuery.Find(&links).Error; err != nil {
		return nil, err
	}
	snapshot := &documentDeleteFiles{links: links}
	fileIDs := []string{}
	documentFiles := map[string]string{}
	for _, link := range links {
		if link.FileID == nil || *link.FileID == "" || link.DocumentID == nil || *link.DocumentID == "" {
			return nil, errors.New("document source file registration is incomplete")
		}
		if previous, exists := documentFiles[*link.DocumentID]; exists && previous != *link.FileID {
			return nil, errors.New("document source file registration is ambiguous")
		}
		documentFiles[*link.DocumentID] = *link.FileID
		fileIDs = append(fileIDs, *link.FileID)
	}
	sort.Strings(fileIDs)
	fileIDs = slices.Compact(fileIDs)
	if len(fileIDs) == 0 {
		return snapshot, nil
	}
	var files []entity.File
	fileQuery := tx.Where("id IN ?", fileIDs).Order("id")
	if lock {
		fileQuery = fileQuery.Clauses(clause.Locking{Strength: "UPDATE"})
	}
	if err := fileQuery.Find(&files).Error; err != nil {
		return nil, err
	}
	if len(files) != len(fileIDs) {
		return nil, errors.New("document source file registration is missing")
	}
	orphans := []entity.File{}
	for _, file := range files {
		if file.TenantID != tenantID {
			return nil, errors.New("document file belongs to another tenant")
		}
		if file.SourceType != string(entity.FileSourceKnowledgebase) {
			continue
		}
		var count int64
		if err := tx.Model(&entity.File2Document{}).Where("file_id = ? AND (document_id NOT IN ? OR document_id IS NULL)", file.ID, ids).Count(&count).Error; err != nil {
			return nil, err
		}
		if count == 0 {
			orphans = append(orphans, file)
		}
	}
	snapshot.files, snapshot.orphans = files, orphans
	return snapshot, nil
}

func (s *DocumentDeleteService) documentDeleteBase(kb *entity.Knowledgebase) (string, error) {
	switch s.engineType {
	case server.EngineElasticsearch, server.EngineType("opensearch"):
		return "multirag_" + kb.TenantID, nil
	case server.EngineInfinity:
		return "multirag_" + kb.TenantID + "_" + kb.Name, nil
	default:
		return "", errors.New("document deletion is unavailable for this Go engine")
	}
}

func (s *DocumentDeleteService) documentDeleteObjects(ctx context.Context, tx *gorm.DB, kb *entity.Knowledgebase, documents []entity.Document, tasks []entity.Task, files []entity.File, ids []string) ([]documentDeleteObject, error) {
	objects := map[documentDeleteObject]struct{}{}
	add := func(bucket, key string) {
		if bucket != "" && key != "" {
			objects[documentDeleteObject{bucket, key}] = struct{}{}
		}
	}
	for _, doc := range documents {
		if doc.Location != nil {
			add(doc.KbID, *doc.Location)
		}
		if doc.Thumbnail != nil && !strings.HasPrefix(*doc.Thumbnail, "data:") {
			add(doc.KbID, *doc.Thumbnail)
		}
	}
	for _, file := range files {
		if file.Location != nil {
			add(kb.ID, *file.Location)
		}
	}
	if s.index == nil {
		return nil, errors.New("document index unavailable")
	}
	base, err := s.documentDeleteBase(kb)
	if err != nil {
		return nil, err
	}
	exists, err := s.index.ChunkStoreExists(ctx, base, kb.ID)
	if err != nil {
		return nil, err
	}
	if exists {
		err = s.walkDocumentDeleteRows(ctx, kb, map[string]any{"doc_id": ids}, func(row map[string]any) error {
			if image, ok := row["img_id"].(string); ok && image != "" {
				bucket, key, valid := strings.Cut(image, "-")
				if !valid || bucket != kb.ID || key == "" {
					return errors.New("document image descriptor is outside dataset")
				}
				add(bucket, key)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	for _, task := range tasks {
		members, err := s.redis.SMembers(ctx, "document-task-images:"+task.ID).Result()
		if err != nil {
			return nil, err
		}
		for _, member := range members {
			var key []string
			if err := json.Unmarshal([]byte(member), &key); err != nil || len(key) != 2 || key[0] != kb.ID || key[1] == "" {
				return nil, errors.New("task image descriptor unavailable")
			}
			add(key[0], key[1])
		}
	}
	result := make([]documentDeleteObject, 0, len(objects))
	for key := range objects {
		if !validDocumentDeleteObject(key) {
			return nil, errors.New("document object address is invalid")
		}
		result = append(result, key)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].bucket != result[j].bucket {
			return result[i].bucket < result[j].bucket
		}
		return result[i].key < result[j].key
	})
	return result, nil
}

func validDocumentDeleteObject(object documentDeleteObject) bool {
	if object.bucket == "" || object.bucket == "." || object.bucket == ".." || strings.ContainsAny(object.bucket, "/\\") || strings.ContainsFunc(object.bucket, unicode.IsControl) {
		return false
	}
	if object.key == "" || strings.HasPrefix(object.key, "/") || strings.Contains(object.key, "\\") || strings.ContainsFunc(object.key, unicode.IsControl) {
		return false
	}
	for _, part := range strings.Split(object.key, "/") {
		if part == "." || part == ".." {
			return false
		}
	}
	return true
}

func lockDocumentDeleteObjects(tx *gorm.DB, objects []documentDeleteObject) error {
	if len(objects) == 0 {
		return nil
	}
	if tx.Dialector.Name() != "postgres" {
		return errors.New("cross-producer image locks unavailable for SQL dialect")
	}
	for _, object := range objects {
		digest := sha256.Sum256([]byte("document-image\x00" + object.bucket + "\x00" + object.key))
		key := int64(binary.BigEndian.Uint64(digest[:8]))
		if err := tx.Exec("SELECT pg_advisory_xact_lock(?)", key).Error; err != nil {
			return err
		}
	}
	return nil
}

func (s *DocumentDeleteService) walkDocumentDeleteRows(ctx context.Context, kb *entity.Knowledgebase, filter map[string]any, visit func(map[string]any) error) error {
	base, err := s.documentDeleteBase(kb)
	if err != nil {
		return err
	}
	var expected int64 = -1
	for offset := 0; ; {
		result, err := s.index.Search(ctx, &types.SearchRequest{IndexNames: []string{base}, KbIDs: []string{kb.ID}, Filter: filter, Offset: offset, Limit: documentDeletePageSize, SelectFields: []string{"id", "doc_id", "kb_id", "img_id", "source_id", "knowledge_graph_kwd", "removed_kwd"}, IncludeUnavailable: true, OrderBy: (&types.OrderByExpr{}).Asc("id")})
		if err != nil {
			return err
		}
		if result == nil || result.Total < 0 || int64(len(result.Chunks)) > result.Total || len(result.Chunks) > documentDeletePageSize {
			return errors.New("invalid document index page")
		}
		if expected == -1 {
			expected = result.Total
		}
		if expected != result.Total {
			return errors.New("document references changed during enumeration")
		}
		if len(result.Chunks) == 0 && int64(offset) < expected {
			return errors.New("incomplete document index page")
		}
		for _, row := range result.Chunks {
			if err := visit(row); err != nil {
				return err
			}
		}
		offset += len(result.Chunks)
		if int64(offset) >= expected {
			return nil
		}
	}
}

func (s *DocumentDeleteService) unreferencedDocumentObjects(ctx context.Context, tx *gorm.DB, kb *entity.Knowledgebase, objects []documentDeleteObject, ids []string, orphanFiles []entity.File) ([]documentDeleteObject, error) {
	if len(objects) == 0 {
		return objects, nil
	}
	protected := map[documentDeleteObject]bool{}
	orphanIDs := []string{}
	for _, file := range orphanFiles {
		orphanIDs = append(orphanIDs, file.ID)
	}
	var files []entity.File
	query := tx.Where("location IS NOT NULL")
	if len(orphanIDs) > 0 {
		query = query.Where("id NOT IN ?", orphanIDs)
	}
	if err := query.Find(&files).Error; err != nil {
		return nil, err
	}
	for _, file := range files {
		if file.Location != nil {
			protected[documentDeleteObject{file.ParentID, *file.Location}] = true
		}
	}
	var otherDocs []entity.Document
	if err := tx.Where("id NOT IN ?", ids).Find(&otherDocs).Error; err != nil {
		return nil, err
	}
	for _, doc := range otherDocs {
		if doc.Location != nil {
			protected[documentDeleteObject{doc.KbID, *doc.Location}] = true
		}
		if doc.Thumbnail != nil {
			protected[documentDeleteObject{doc.KbID, *doc.Thumbnail}] = true
		}
	}
	// A shared original source remains registered by another File2Document,
	// including a local file whose bucket differs from the dataset namespace.
	var shared []struct{ DocumentID string }
	if err := tx.Table("t_ai_file2documents AS link").Select("link.document_id").Joins("JOIN t_ai_files AS f ON f.id=link.file_id").Where("link.document_id IN ? AND (f.source_type <> ? OR EXISTS (SELECT 1 FROM t_ai_file2documents other WHERE other.file_id=f.id AND (other.document_id NOT IN ? OR other.document_id IS NULL)))", ids, string(entity.FileSourceKnowledgebase), ids).Find(&shared).Error; err != nil {
		return nil, err
	}
	sharedDocs := map[string]bool{}
	for _, row := range shared {
		sharedDocs[row.DocumentID] = true
	}
	for _, id := range ids {
		if !sharedDocs[id] {
			continue
		}
		var doc entity.Document
		if err := tx.Where("id=?", id).Take(&doc).Error; err != nil {
			return nil, err
		}
		if doc.Location != nil {
			protected[documentDeleteObject{doc.KbID, *doc.Location}] = true
		}
	}
	candidates := make([]string, 0, len(objects))
	for _, object := range objects {
		candidates = append(candidates, object.bucket+"-"+object.key)
	}
	selected := map[string]bool{}
	for _, id := range ids {
		selected[id] = true
	}
	var datasets []entity.Knowledgebase
	if err := tx.Order("id").Find(&datasets).Error; err != nil {
		return nil, err
	}
	for _, dataset := range datasets {
		base, err := s.documentDeleteBase(&dataset)
		if err != nil {
			return nil, err
		}
		exists, err := s.index.ChunkStoreExists(ctx, base, dataset.ID)
		if err != nil {
			return nil, err
		}
		if !exists {
			continue
		}
		if err := s.walkDocumentDeleteRows(ctx, &dataset, map[string]any{"img_id": candidates}, func(row map[string]any) error {
			image, ok := row["img_id"].(string)
			if !ok {
				return errors.New("image reference unavailable")
			}
			doc, _ := row["doc_id"].(string)
			if dataset.ID == kb.ID && selected[doc] {
				return nil
			}
			bucket, key, ok := strings.Cut(image, "-")
			if !ok {
				return errors.New("image reference unavailable")
			}
			protected[documentDeleteObject{bucket, key}] = true
			return nil
		}); err != nil {
			return nil, err
		}
	}
	var pending []struct{ ID string }
	if err := tx.Table("t_ai_tasks AS task").Select("task.id").Joins("JOIN t_ai_documents doc ON doc.id=task.doc_id").Where("doc.id NOT IN ? AND task.progress >= 0 AND task.progress < 1 AND (doc.run IS NULL OR doc.run NOT IN ?) AND POSITION(? IN COALESCE(task.progress_msg,'')) = 0", ids, []string{"0", "2"}, taskCancelMarker).Find(&pending).Error; err != nil {
		return nil, err
	}
	if len(pending) > 0 && s.redis == nil {
		return nil, errors.New("pending image references unavailable")
	}
	for _, task := range pending {
		members, err := s.redis.SMembers(ctx, "document-task-images:"+task.ID).Result()
		if err != nil {
			return nil, err
		}
		for _, member := range members {
			var key []string
			if err := json.Unmarshal([]byte(member), &key); err != nil || len(key) != 2 || key[0] == "" || key[1] == "" {
				return nil, errors.New("pending image descriptor unavailable")
			}
			protected[documentDeleteObject{key[0], key[1]}] = true
		}
	}
	result := make([]documentDeleteObject, 0, len(objects))
	for _, object := range objects {
		if !protected[object] {
			result = append(result, object)
		}
	}
	return result, nil
}

func (s *DocumentDeleteService) deleteDocumentIndexes(ctx context.Context, kb *entity.Knowledgebase, ids []string, documents []entity.Document, lease *documentGraphDeleteLease) error {
	base, err := s.documentDeleteBase(kb)
	if err != nil {
		return err
	}
	exists, err := s.index.ChunkStoreExists(ctx, base, kb.ID)
	if err != nil {
		return err
	}
	if exists {
		for start := 0; start < len(ids); start += documentDeletePageSize {
			if err := lease.check(ctx); err != nil {
				return err
			}
			end := min(start+documentDeletePageSize, len(ids))
			count, err := s.index.DeleteChunks(ctx, map[string]any{"doc_id": ids[start:end]}, base, kb.ID)
			if err != nil {
				return err
			}
			if count < 0 {
				return errors.New("invalid chunk deletion acknowledgement")
			}
		}
		if err := s.walkDocumentDeleteRows(ctx, kb, map[string]any{"doc_id": ids}, func(map[string]any) error { return errors.New("document chunks remain after deletion") }); err != nil {
			return err
		}
	} else {
		for _, doc := range documents {
			if doc.ChunkNum > 0 {
				return errors.New("indexed document store unavailable")
			}
		}
	}
	exists, err = s.index.MetadataStoreExists(ctx, kb.TenantID)
	if err != nil {
		return err
	}
	if exists {
		for start := 0; start < len(ids); start += documentDeletePageSize {
			if err := lease.check(ctx); err != nil {
				return err
			}
			end := min(start+documentDeletePageSize, len(ids))
			count, err := s.index.DeleteMetadata(ctx, map[string]any{"id": ids[start:end], "kb_id": kb.ID}, kb.TenantID)
			if err != nil {
				return err
			}
			if count < 0 {
				return errors.New("invalid metadata deletion acknowledgement")
			}
		}
		result, err := s.index.Search(ctx, &types.SearchRequest{IndexNames: []string{BuildMetadataIndexName(kb.TenantID)}, KbIDs: []string{kb.ID}, Filter: map[string]any{"id": ids, "kb_id": kb.ID}, Offset: 0, Limit: 1, SelectFields: []string{"id", "kb_id"}, IncludeUnavailable: true})
		if err != nil {
			return err
		}
		if result == nil || result.Total != 0 || len(result.Chunks) != 0 {
			return errors.New("document metadata remains after deletion")
		}
	}
	return nil
}

type documentDeleteCommittedError struct{ cause error }

func (e *documentDeleteCommittedError) Error() string { return ErrDocumentDeleteCommitted.Error() }
func (e *documentDeleteCommittedError) Unwrap() []error {
	return []error{ErrDocumentDeleteCommitted, e.cause}
}
func fmtDocumentDeleteCommitted(cause error) error {
	return &documentDeleteCommittedError{cause: cause}
}

type documentGraphDeleteLease struct {
	redis      *redis.Client
	key, token string
	stop       chan struct{}
	done       chan struct{}
	mu         sync.Mutex
	failure    error
}

func (s *DocumentDeleteService) claimDocumentGraphLease(ctx context.Context, tenantID, datasetID string) (*documentGraphDeleteLease, error) {
	if s.redis == nil {
		return nil, errors.New("graph writer coordination unavailable")
	}
	lease := &documentGraphDeleteLease{redis: s.redis, key: "graphrag:writer:" + tenantID + ":" + datasetID, token: uuid.NewString(), stop: make(chan struct{}), done: make(chan struct{})}
	written, err := s.redis.SetNX(ctx, lease.key, lease.token, 20*time.Minute).Result()
	if err != nil {
		// An acknowledgement can be lost after SET. Remove only this token.
		cleanup, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, cleanupErr := s.redis.Eval(cleanup, deleteCancelScript, []string{lease.key}, lease.token).Result()
		return nil, errors.Join(err, cleanupErr)
	}
	if !written {
		return nil, errors.New("dataset graph writer is active")
	}
	go func() {
		defer close(lease.done)
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-lease.stop:
				return
			case <-ticker.C:
				renew, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				ack, err := lease.redis.Eval(renew, `if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end redis.call('EXPIRE',KEYS[1],ARGV[2]) return 1`, []string{lease.key}, lease.token, 1200).Int()
				cancel()
				if err != nil || ack != 1 {
					lease.mu.Lock()
					lease.failure = errors.New("graph writer lease renewal failed")
					lease.mu.Unlock()
					return
				}
			}
		}
	}()
	return lease, nil
}

func (l *documentGraphDeleteLease) check(ctx context.Context) error {
	l.mu.Lock()
	failure := l.failure
	l.mu.Unlock()
	if failure != nil {
		return failure
	}
	value, err := l.redis.Get(ctx, l.key).Result()
	if err != nil {
		return err
	}
	if value != l.token {
		return errors.New("graph writer lease ownership lost")
	}
	return nil
}
func (l *documentGraphDeleteLease) release() error {
	close(l.stop)
	<-l.done
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ack, err := l.redis.Eval(ctx, deleteCancelScript, []string{l.key}, l.token).Int()
	if err != nil {
		return err
	}
	if ack != 1 {
		return errors.New("owned graph writer lease changed")
	}
	return nil
}

func documentGraphSources(value any) ([]string, error) {
	var values []string
	switch raw := value.(type) {
	case nil:
		return nil, nil
	case string:
		if raw == "" {
			return nil, nil
		}
		if strings.Contains(raw, "###") {
			values = strings.Split(raw, "###")
		} else {
			values = strings.Split(raw, "\n")
		}
	case []string:
		values = append(values, raw...)
	case []any:
		for _, item := range raw {
			text, ok := item.(string)
			if !ok {
				return nil, errors.New("invalid graph source identity")
			}
			values = append(values, text)
		}
	default:
		return nil, errors.New("invalid graph source provenance")
	}
	for _, value := range values {
		if value == "" {
			return nil, errors.New("empty graph source identity")
		}
	}
	sort.Strings(values)
	return slices.Compact(values), nil
}

func (s *DocumentDeleteService) deleteDocumentGraph(ctx context.Context, kb *entity.Knowledgebase, ids []string, lease *documentGraphDeleteLease) error {
	base, err := s.documentDeleteBase(kb)
	if err != nil {
		return err
	}
	exists, err := s.index.ChunkStoreExists(ctx, base, kb.ID)
	if err != nil {
		return err
	}
	if !exists {
		return nil
	}
	kinds := []string{"graph", "subgraph", "entity", "relation", "community_report"}
	selected := map[string]bool{}
	for _, id := range ids {
		selected[id] = true
	}
	rows := []map[string]any{}
	if err := s.walkDocumentDeleteRows(ctx, kb, map[string]any{"knowledge_graph_kwd": kinds}, func(row map[string]any) error { rows = append(rows, row); return nil }); err != nil {
		return err
	}
	// Invalidate before changing provenance. A lost update acknowledgement can
	// otherwise leave a retry with no old source_id to detect, while the cached
	// aggregate still contains the removed document's content and remains N.
	if len(rows) > 0 {
		if err := lease.check(ctx); err != nil {
			return err
		}
		if err := s.index.UpdateChunks(ctx, map[string]any{"knowledge_graph_kwd": "graph"}, map[string]any{"removed_kwd": "Y"}, base, kb.ID); err != nil {
			return err
		}
		if err := s.walkDocumentDeleteRows(ctx, kb, map[string]any{"knowledge_graph_kwd": "graph"}, func(row map[string]any) error {
			if row["removed_kwd"] != "Y" {
				return errors.New("graph invalidation readback mismatch")
			}
			return nil
		}); err != nil {
			return err
		}
	}
	for _, row := range rows {
		if err := lease.check(ctx); err != nil {
			return err
		}
		sources, err := documentGraphSources(row["source_id"])
		if err != nil {
			return err
		}
		remaining := []string{}
		removed := []string{}
		for _, source := range sources {
			if selected[source] {
				removed = append(removed, source)
			} else {
				remaining = append(remaining, source)
			}
		}
		if len(removed) == 0 {
			continue
		}
		id, ok := row["id"].(string)
		if !ok || id == "" {
			return errors.New("graph row identity unavailable")
		}
		condition := map[string]any{"id": id, "knowledge_graph_kwd": kinds}
		if len(remaining) == 0 {
			ack, err := s.index.DeleteChunks(ctx, condition, base, kb.ID)
			if err != nil {
				return err
			}
			if ack < 0 {
				return errors.New("invalid graph deletion acknowledgement")
			}
		} else {
			for _, source := range removed {
				if err := s.index.UpdateChunks(ctx, condition, map[string]any{"remove": map[string]any{"source_id": source}}, base, kb.ID); err != nil {
					return err
				}
			}
		}
		found := 0
		if err := s.walkDocumentDeleteRows(ctx, kb, map[string]any{"id": id, "knowledge_graph_kwd": kinds}, func(actual map[string]any) error {
			found++
			actualSources, err := documentGraphSources(actual["source_id"])
			if err != nil {
				return err
			}
			if !slices.Equal(actualSources, remaining) {
				return errors.New("graph provenance update readback mismatch")
			}
			return nil
		}); err != nil {
			return err
		}
		if (len(remaining) == 0 && found != 0) || (len(remaining) > 0 && found != 1) {
			return errors.New("graph cleanup readback incomplete")
		}
	}
	return nil
}

func validateDocumentSourceRecovery(record documentDeleteRecovery) error {
	envelope, err := strictJSONObject([]byte(record.Wire))
	if err != nil {
		return errors.New("source recovery wire unavailable")
	}
	fields, err := strictJSONObject(envelope["payload"])
	if err != nil {
		return errors.New("source recovery payload unavailable")
	}
	var payload struct {
		Version    int    `json:"version"`
		DocumentID string `json:"document_id"`
		TaskID     string `json:"task_id"`
		Nonce      string `json:"nonce"`
		Phase      string `json:"phase"`
	}
	if err := json.Unmarshal(envelope["payload"], &payload); err != nil {
		return errors.New("source recovery identity unavailable")
	}
	if payload.Version != 1 || payload.DocumentID != record.DocumentID || payload.TaskID != record.TaskID || payload.Nonce != record.ID || payload.TaskID == "" || payload.Phase != "ready" {
		return errors.New("source recovery requires reconciliation before deletion")
	}
	rawTypes := bytes.TrimSpace(envelope["types"])
	if len(rawTypes) == 0 || rawTypes[0] != '[' {
		return errors.New("source recovery type tags unavailable")
	}
	var tags []json.RawMessage
	if err := json.Unmarshal(rawTypes, &tags); err != nil {
		return errors.New("source recovery type tags unavailable")
	}
	for _, tag := range tags {
		entry, err := strictJSONObject(tag)
		if err != nil {
			return errors.New("invalid source recovery type tag")
		}
		var kind string
		var path []json.RawMessage
		if err := json.Unmarshal(entry["type"], &kind); err != nil {
			return errors.New("invalid source recovery type name")
		}
		if err := json.Unmarshal(entry["path"], &path); err != nil || len(path) == 0 {
			return errors.New("invalid source recovery type path")
		}
		current := json.RawMessage(envelope["payload"])
		for _, part := range path {
			var key string
			if err := json.Unmarshal(part, &key); err == nil {
				object, err := strictJSONObject(current)
				if err != nil {
					return errors.New("invalid source recovery object path")
				}
				var exists bool
				current, exists = object[key]
				if !exists {
					return errors.New("source recovery object path is missing")
				}
			} else {
				var index int
				var values []json.RawMessage
				if err := json.Unmarshal(part, &index); err != nil || index < 0 {
					return errors.New("invalid source recovery array index")
				}
				if err := json.Unmarshal(current, &values); err != nil || index >= len(values) {
					return errors.New("source recovery array path is missing")
				}
				current = values[index]
			}
		}
		var value string
		if err := json.Unmarshal(current, &value); err != nil {
			return errors.New("source recovery typed value is invalid")
		}
		switch kind {
		case "datetime":
			if _, err := time.Parse(time.RFC3339Nano, value); err != nil {
				if _, err := time.Parse("2006-01-02T15:04:05.999999999", value); err != nil {
					return errors.New("source recovery datetime is invalid")
				}
			}
		case "date":
			if _, err := time.Parse("2006-01-02", value); err != nil {
				return errors.New("source recovery date is invalid")
			}
		case "bytes":
			if _, err := base64.StdEncoding.DecodeString(value); err != nil {
				return errors.New("source recovery bytes are invalid")
			}
		case "decimal":
			if _, ok := new(big.Rat).SetString(value); !ok {
				return errors.New("source recovery decimal is invalid")
			}
		default:
			return errors.New("source recovery type is unsupported")
		}
	}
	// Identity fields must not be reconstructed as another type by Python's
	// _read_wire; they are the exact scalar values used to retire the SQL row.
	for _, tag := range tags {
		var entry struct {
			Path []any `json:"path"`
		}
		if err := json.Unmarshal(tag, &entry); err != nil {
			return err
		}
		if len(entry.Path) == 1 {
			if key, ok := entry.Path[0].(string); ok {
				if _, identity := fields[key]; identity && (key == "version" || key == "document_id" || key == "task_id" || key == "nonce" || key == "phase") {
					return errors.New("source recovery identity cannot carry a type tag")
				}
			}
		}
	}
	return nil
}
