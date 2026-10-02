package service

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/server"
)

var ErrDocumentStatusForbidden = errors.New("dataset unavailable or no authorization")

const documentStatusError = "Failed to update document status; retry to reconcile."
const documentStatusRecoveryError = "Document status recovery could not be confirmed; retry to reconcile."

// NewDocumentStatusService supplies explicit storage dependencies for isolated hosts.
func NewDocumentStatusService(db *gorm.DB, store engine.DocEngine, engineType server.EngineType) *DocumentService {
	return &DocumentService{db: db, documentDAO: dao.NewDocumentDAO(), docEngine: store, engineType: engineType}
}

func writableStatusDataset(tx *gorm.DB, datasetID, userID string) (*entity.Knowledgebase, error) {
	var kb entity.Knowledgebase
	if err := tx.Where("id = ? AND status = ?", datasetID, "1").Take(&kb).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrDocumentStatusForbidden
		}
		return nil, err
	}
	if kb.TenantID == userID {
		return &kb, nil
	}
	var count int64
	if err := tx.Table("t_ai_user_tenants").Where("tenant_id = ? AND user_id = ? AND status = ? AND role IN ?", kb.TenantID, userID, "1", []string{"owner", "admin"}).Count(&count).Error; err != nil {
		return nil, err
	}
	if count != 1 {
		return nil, ErrDocumentStatusForbidden
	}
	return &kb, nil
}

// BatchUpdateStatus returns one outcome per distinct ID, in independent transactions.
func (s *DocumentService) BatchUpdateStatus(ctx context.Context, datasetID, userID string, ids []string, status string) (map[string]map[string]string, error) {
	if status != "0" && status != "1" {
		return nil, errors.New("invalid status")
	}
	// Once accepted, drain the store operation before releasing its SQL lock, even
	// when the HTTP client disconnects. Infinity RPC does not consume context;
	// cancelling SQL first would release its lock while that write still runs.
	owned := context.WithoutCancel(ctx)
	if _, err := writableStatusDataset(s.db.WithContext(owned), datasetID, userID); err != nil {
		return nil, err
	}
	results := map[string]map[string]string{}
	for _, id := range ids {
		if _, exists := results[id]; exists {
			continue
		}
		if msg := s.changeDocumentStatus(owned, datasetID, userID, id, status); msg != "" {
			results[id] = map[string]string{"error": msg}
		} else {
			results[id] = map[string]string{"status": status}
		}
	}
	return results, nil
}

func (s *DocumentService) changeDocumentStatus(ctx context.Context, datasetID, userID, id, status string) string {
	tx := s.db.WithContext(ctx).Begin()
	if tx.Error != nil {
		return documentStatusError
	}
	var doc entity.Document
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND kb_id = ?", id, datasetID).Take(&doc).Error; err != nil {
		tx.Rollback()
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return "Document not found in this dataset."
		}
		return documentStatusError
	}
	kb, err := writableStatusDataset(tx, datasetID, userID)
	if err != nil {
		tx.Rollback()
		return "No authorization."
	}
	prefix := fmt.Sprintf("multirag_%s_%s", kb.TenantID, kb.Name)
	indexed := doc.ChunkNum > 0
	if s.engineType == server.EngineInfinity && s.docEngine != nil {
		counter, ok := s.docEngine.(interface {
			DocumentChunkCount(context.Context, string, string, string) (int64, error)
		})
		if !ok {
			err = errors.New("index count unavailable")
		} else {
			var count int64
			count, err = counter.DocumentChunkCount(ctx, prefix, datasetID, id)
			indexed = count > 0
			if doc.ChunkNum > 0 && count == 0 && err == nil {
				err = errors.New("indexed document rows unavailable")
			}
		}
	} else if !indexed && s.docEngine != nil {
		indexed, err = s.docEngine.TableExists(ctx, prefix)
	}

	if err == nil && indexed {
		if s.engineType != server.EngineInfinity || s.docEngine == nil {
			tx.Rollback()
			return "Document status index updates are unavailable for this Go engine."
		}
		err = s.docEngine.UpdateDataset(ctx, map[string]interface{}{"doc_id": id}, map[string]interface{}{"available_int": statusInt(status)}, prefix, datasetID)
	}
	if err == nil {
		err = s.documentDAO.UpdateStatus(tx, id, datasetID, status)
	}
	if err == nil {
		err = tx.Commit().Error
	}
	if err == nil {
		return ""
	}
	if rollback := tx.Rollback().Error; rollback != nil && !errors.Is(rollback, gorm.ErrInvalidTransaction) && !errors.Is(rollback, sql.ErrTxDone) {
		return documentStatusRecoveryError
	}
	if indexed {
		// Re-lock and restore the current SQL winner after an uncertain write/commit.
		recovery := s.db.WithContext(context.WithoutCancel(ctx)).Begin()
		if recovery.Error != nil {
			return documentStatusRecoveryError
		}
		defer recovery.Rollback()
		var current entity.Document
		if recovery.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND kb_id = ?", id, datasetID).Take(&current).Error != nil {
			return documentStatusRecoveryError
		}
		target := "1"
		if current.Status != nil && *current.Status == "0" {
			target = "0"
		}
		if s.docEngine.UpdateDataset(context.WithoutCancel(ctx), map[string]interface{}{"doc_id": id}, map[string]interface{}{"available_int": statusInt(target)}, prefix, datasetID) != nil {
			return documentStatusRecoveryError
		}
		if recovery.Rollback().Error != nil {
			return documentStatusRecoveryError
		}
	}
	return documentStatusError
}
func statusInt(status string) int {
	if status == "0" {
		return 0
	}
	return 1
}
