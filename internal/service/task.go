package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"multirag/internal/entity"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

var ErrTaskForbidden = errors.New("no authorization to cancel this task")

const taskRuntimePrefix = "task-runtime:v1:"
const taskRuntimeTTL = 24 * time.Hour
const taskCancelMarker = "[cancel_requested]"

// These scripts implement core/utils/task_runtime.py's version 1 protocol.
const cancelRuntimeScript = `
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local binding = cjson.decode(ARGV[1])
if binding.state ~= 'active' then return 0 end
binding.state = 'cancel_requested'
binding.cancel_token = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(binding), 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1`
const restoreRuntimeScript = `
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local binding = cjson.decode(raw)
if binding.cancel_token ~= ARGV[2] then return 0 end
if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('DEL', KEYS[2])
return 1`
const deleteCancelScript = `
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1`

type taskBinding struct {
	Version     int    `json:"version"`
	PrincipalID string `json:"principal_id"`
	TenantID    string `json:"tenant_id"`
	ResourceID  string `json:"resource_id"`
	Kind        string `json:"kind"`
	State       string `json:"state"`
}

type TaskService struct {
	db    *gorm.DB
	redis *redis.Client
}

func NewTaskService(db *gorm.DB, redisClient *redis.Client) *TaskService {
	return &TaskService{db: db, redis: redisClient}
}

func (s *TaskService) readBinding(ctx context.Context, taskID string) (string, *taskBinding, error) {
	raw, err := s.redis.Get(ctx, taskRuntimePrefix+taskID).Result()
	if errors.Is(err, redis.Nil) {
		return "", nil, nil
	}
	if err != nil {
		return "", nil, err
	}
	var binding taskBinding
	if err := json.Unmarshal([]byte(raw), &binding); err != nil {
		return "", nil, err
	}
	if binding.Version != 1 || binding.PrincipalID == "" || binding.TenantID == "" || binding.ResourceID == "" ||
		(binding.Kind != "agent" && binding.Kind != "dataflow") ||
		(binding.State != "active" && binding.State != "cancel_requested" && binding.State != "finished") {
		return "", nil, errors.New("invalid task runtime binding")
	}
	return raw, &binding, nil
}

func requireTaskMembership(tx *gorm.DB, tenantID, userID string) error {
	var count int64
	if err := tx.Table("t_ai_user_tenants").Where("tenant_id = ? AND user_id = ? AND status = ? AND role IN ?", tenantID, userID, "1", []string{"owner", "normal", "admin"}).Count(&count).Error; err != nil {
		return err
	}
	if count == 0 {
		return ErrTaskForbidden
	}
	return nil
}

func authorizeTaskBinding(tx *gorm.DB, binding *taskBinding, userID string) error {
	var canvas struct {
		UserID     string
		Permission string
	}
	result := tx.Table("t_ai_user_canvases").Select("user_id, permission").Where("id = ?", binding.ResourceID).Take(&canvas)
	if errors.Is(result.Error, gorm.ErrRecordNotFound) || (result.Error == nil && canvas.UserID != binding.TenantID) {
		return ErrTaskForbidden
	}
	if result.Error != nil {
		return result.Error
	}
	if userID == binding.TenantID {
		return nil
	}
	if canvas.Permission != "team" {
		return ErrTaskForbidden
	}
	return requireTaskMembership(tx, binding.TenantID, userID)
}

// Cancel commits a cancellation request. It does not prove worker termination.
func (s *TaskService) Cancel(ctx context.Context, taskID, userID string) error {
	if s.db == nil || s.redis == nil {
		return errors.New("task cancellation storage is unavailable")
	}
	token := uuid.NewString()
	flagWritten := false
	previousBinding := ""
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var task entity.Task
		taskErr := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", taskID).Take(&task).Error
		hasTask := taskErr == nil
		if taskErr != nil && !errors.Is(taskErr, gorm.ErrRecordNotFound) {
			return taskErr
		}
		raw, binding, err := s.readBinding(ctx, taskID)
		if err != nil {
			return err
		}
		docID := ""
		if !hasTask {
			if binding == nil {
				return nil
			}
			if err := authorizeTaskBinding(tx, binding, userID); err != nil {
				return err
			}
		} else if task.DocID == "dataflow_x" {
			if binding == nil || binding.Kind != "dataflow" {
				return ErrTaskForbidden
			}
			if err := authorizeTaskBinding(tx, binding, userID); err != nil {
				return err
			}
		} else {
			var kb struct{ TenantID string }
			var query *gorm.DB
			if task.DocID == "graph_raptor_x" {
				query = tx.Table("t_ai_knowledgebases").Select("tenant_id").Where("status = ? AND (graphrag_task_id = ? OR raptor_task_id = ? OR mindmap_task_id = ?)", "1", taskID, taskID, taskID)
			} else {
				var doc struct{ KBID string }
				result := tx.Table("t_ai_documents").Select("kb_id").Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ?", task.DocID, "1").Take(&doc)
				if errors.Is(result.Error, gorm.ErrRecordNotFound) {
					return ErrTaskForbidden
				}
				if result.Error != nil {
					return result.Error
				}
				docID = task.DocID
				query = tx.Table("t_ai_knowledgebases").Select("tenant_id").Where("id = ? AND status = ?", doc.KBID, "1")
			}
			if err := query.Take(&kb).Error; err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					return ErrTaskForbidden
				}
				return err
			}
			if err := requireTaskMembership(tx, kb.TenantID, userID); err != nil {
				return err
			}
		}
		if (hasTask && (task.Progress < 0 || task.Progress >= 1)) || (binding != nil && binding.State != "active") {
			return nil
		}
		if binding != nil {
			previousBinding = raw
			written, err := s.redis.Eval(ctx, cancelRuntimeScript, []string{taskRuntimePrefix + taskID, taskID + "-cancel"}, raw, token, int(taskRuntimeTTL.Seconds())).Int()
			if err != nil {
				return err
			}
			flagWritten = written == 1
			if !flagWritten {
				_, current, err := s.readBinding(ctx, taskID)
				if err != nil {
					return err
				}
				if current != nil {
					if err := authorizeTaskBinding(tx, current, userID); err != nil {
						return err
					}
					if current.State == "active" {
						return errors.New("task ownership changed; retry cancellation")
					}
				}
				return nil
			}
		} else {
			if err := s.redis.Set(ctx, taskID+"-cancel", token, taskRuntimeTTL).Err(); err != nil {
				return err
			}
			flagWritten = true
		}
		if hasTask {
			message := "\n" + time.Now().Format("15:04:05") + " " + taskCancelMarker + " Task stopped by user."
			if err := tx.Model(&entity.Task{}).Where("id = ? AND progress >= 0 AND progress < 1", taskID).Updates(map[string]interface{}{
				"progress": -1, "progress_msg": gorm.Expr("COALESCE(progress_msg, '') || ?", message),
			}).Error; err != nil {
				return err
			}
			if docID != "" {
				if err := tx.Table("t_ai_documents").Where("id = ? AND run IN ?", docID, []string{"1", "5"}).Updates(map[string]interface{}{
					"run": "2", "progress": 0, "progress_msg": gorm.Expr("COALESCE(progress_msg, '') || ?", message),
				}).Error; err != nil {
					return err
				}
			}
		}
		return nil
	})
	if err != nil && flagWritten {
		// The HTTP context may be cancelled; compensation gets its own deadline.
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var restored int
		var cleanupErr error
		if previousBinding != "" {
			restored, cleanupErr = s.redis.Eval(cleanupCtx, restoreRuntimeScript, []string{taskRuntimePrefix + taskID, taskID + "-cancel"}, previousBinding, token, int(taskRuntimeTTL.Seconds())).Int()
		} else {
			restored, cleanupErr = s.redis.Eval(cleanupCtx, deleteCancelScript, []string{taskID + "-cancel"}, token).Int()
		}
		if cleanupErr != nil || restored != 1 {
			return fmt.Errorf("cancellation failed; Redis compensation could not be confirmed: %w", err)
		}
	}
	return err
}
