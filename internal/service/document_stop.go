package service

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"multirag/internal/cache"
	"multirag/internal/entity"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
)

var ErrInvalidDocumentStopRequest = errors.New("document_ids must be a nonempty array of document IDs")
var ErrDocumentStopInactive = errors.New("document has no active parsing task to cancel")
var ErrDocumentStopUncertain = errors.New("SQL cancellation commit could not be confirmed; cancellation fences retained")

// DocumentStopService accepts cancellation atomically; workers drain separately.
// It never removes chunks, objects, queue entries or parent rows.
type DocumentStopService struct {
	db    *gorm.DB
	redis *redis.Client
}

func NewDocumentStopService(db *gorm.DB, client *redis.Client) *DocumentStopService {
	return &DocumentStopService{db: db, redis: client}
}

func (s *DocumentService) SetDocumentStopService(stopping *DocumentStopService) {
	s.stopping = stopping
}

func (s *DocumentService) StopParseDocuments(ctx context.Context, datasetID, userID string, ids []string) (int, error) {
	if s.stopping != nil {
		return s.stopping.StopParseDocuments(ctx, datasetID, userID, ids)
	}
	client := cache.Get()
	var redisClient *redis.Client
	if client != nil {
		redisClient = client.GetClient()
	}
	return NewDocumentStopService(s.db, redisClient).StopParseDocuments(ctx, datasetID, userID, ids)
}

func (s *DocumentStopService) StopParseDocuments(ctx context.Context, datasetID, userID string, ids []string) (int, error) {
	selected, err := normalizedDocumentDeleteIDs(ids, false)
	if err != nil || len(selected) == 0 {
		return 0, ErrInvalidDocumentStopRequest
	}
	if datasetID == "" || userID == "" {
		return 0, ErrDocumentDeleteForbidden
	}
	if s == nil || s.db == nil {
		return 0, errors.New("document cancellation storage unavailable")
	}
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	receipts := []documentCancelReceipt{}
	commitAttempted := false
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		// All producers/deletion lock the whole document batch before any task.
		documents, tasks, err := lockedDeleteDocuments(tx, datasetID, selected)
		if err != nil {
			return err
		}
		kb, err := authorizeDocumentDelete(tx, datasetID, userID, true)
		if err != nil {
			return err
		}
		byID := make(map[string]entity.Document, len(documents))
		for _, doc := range documents {
			byID[doc.ID] = doc
		}
		active := []entity.Task{}
		bindings := map[string]string{}
		activeDocuments := map[string]bool{}
		for _, task := range tasks {
			if task.Progress < 0 || task.Progress >= 1 {
				continue
			}
			if s.redis == nil {
				return errors.New("task cancellation unavailable")
			}
			raw, binding, err := (&TaskService{redis: s.redis}).readBinding(ctx, task.ID)
			if err != nil {
				return err
			}
			if binding != nil {
				doc := byID[task.DocID]
				if binding.TenantID != kb.TenantID || binding.ResourceID != doc.ID || binding.PrincipalID != doc.CreatedBy {
					return ErrTaskForbidden
				}
				if binding.State == "finished" {
					return ErrDocumentStopInactive
				}
				if binding.State == "cancel_requested" {
					var receipt struct {
						CancelToken string `json:"cancel_token"`
					}
					if json.Unmarshal([]byte(raw), &receipt) != nil || receipt.CancelToken == "" {
						return errors.New("invalid task cancellation binding")
					}
					flag, err := s.redis.Get(ctx, task.ID+"-cancel").Result()
					if err != nil || flag != receipt.CancelToken {
						return errors.New("task cancellation fence unavailable")
					}
				}
				bindings[task.ID] = raw
			}
			active = append(active, task)
			activeDocuments[task.DocID] = true
		}
		for _, doc := range documents {
			if (doc.Run == nil || (*doc.Run != "1" && *doc.Run != "2")) && !activeDocuments[doc.ID] {
				return ErrDocumentStopInactive
			}
		}
		// Validate every ID and runtime binding before the first external write.
		for _, task := range active {
			receipt := documentCancelReceipt{taskID: task.ID, token: uuid.NewString(), previousBinding: bindings[task.ID]}
			receipts = append(receipts, receipt) // acknowledgement loss still needs CAS compensation
			if receipt.previousBinding != "" {
				var binding taskBinding
				if err := json.Unmarshal([]byte(receipt.previousBinding), &binding); err != nil {
					return err
				}
				if binding.State == "active" {
					n, err := s.redis.Eval(ctx, cancelRuntimeScript, []string{taskRuntimePrefix + task.ID, task.ID + "-cancel"}, receipt.previousBinding, receipt.token, int(taskRuntimeTTL.Seconds())).Int()
					if err != nil {
						return err
					}
					if n != 1 {
						return errors.New("task runtime changed; retry cancellation")
					}
				}
			} else {
				written, err := s.redis.SetNX(ctx, task.ID+"-cancel", receipt.token, taskRuntimeTTL).Result()
				if err != nil {
					return err
				}
				if !written {
					value, err := s.redis.Get(ctx, task.ID+"-cancel").Result()
					if err != nil {
						return err
					}
					if value == "" {
						return errors.New("invalid existing task cancellation")
					}
				}
			}
			message := ""
			if task.ProgressMsg != nil {
				message = *task.ProgressMsg
			}
			if !strings.Contains(message, taskCancelMarker) {
				message += "\n" + taskCancelMarker + " Document stop requested."
			}
			if err := tx.Model(&entity.Task{}).Where("id = ? AND doc_id = ?", task.ID, task.DocID).Updates(map[string]any{"progress": -1, "progress_msg": message}).Error; err != nil {
				return err
			}
		}
		// A canceled retry is a no-op; terminal documents without active tasks were rejected above.
		for _, doc := range documents {
			if doc.Run != nil && *doc.Run == "2" && !activeDocuments[doc.ID] {
				continue
			}
			if err := tx.Model(&entity.Document{}).Where("id = ? AND kb_id = ?", doc.ID, datasetID).Updates(map[string]any{"run": "2", "progress": 0, "update_time": gorm.Expr("GREATEST(COALESCE(update_time, 0) + 1, ?)", time.Now().UnixMilli())}).Error; err != nil {
				return err
			}
		}
		commitAttempted = true
		return nil
	})
	if err != nil {
		// Once COMMIT was attempted its acknowledgement can be lost after SQL
		// persisted. Erasing a worker fence would revive accepted work. Keep
		// the durable nonce and report uncertainty for reconciliation/retry.
		if commitAttempted {
			return 0, errors.Join(ErrDocumentStopUncertain, err)
		}
		if cleanupErr := (&DocumentDeleteService{redis: s.redis}).compensateDocumentCancellation(receipts); cleanupErr != nil {
			return 0, errors.Join(err, cleanupErr)
		}
		return 0, err
	}
	return len(selected), nil
}
