package dao

import (
	"context"
	"fmt"
	"math"
	"multirag/internal/entity"
	"strings"
	"time"
)

// IngestionQuery is a validated, bounded selection of dataset or file logs.
type IngestionQuery struct {
	Page, PageSize        int
	OrderBy               string
	Desc                  bool
	LogType, Keywords     string
	Status, Types, Suffix []string
	From, To              *time.Time
}

var ingestionOrderFields = map[string]bool{}

func init() {
	for _, key := range strings.Fields("id tenant_id kb_id progress progress_msg process_begin_at process_duration task_type operation_status avatar status create_time create_date update_time update_date document_id pipeline_id pipeline_title parser_id document_name document_suffix document_type source_from") {
		ingestionOrderFields[key] = true
	}
}

// Validate rejects invalid selections before SQL and never broadens empty filters.
func (q IngestionQuery) Validate() error {
	if q.Page < 0 || q.PageSize < 0 || q.PageSize > 100 || (q.PageSize > 0 && q.Page > math.MaxInt/q.PageSize) {
		return fmt.Errorf("invalid pagination")
	}
	if !ingestionOrderFields[q.OrderBy] || (q.LogType != "dataset" && q.LogType != "file") {
		return fmt.Errorf("invalid log type or orderby")
	}
	if q.LogType == "dataset" && strings.Contains(" document_id pipeline_id pipeline_title parser_id document_name document_suffix document_type source_from ", " "+q.OrderBy+" ") {
		return fmt.Errorf("invalid dataset orderby")
	}
	if q.From != nil && q.To != nil && q.From.After(*q.To) {
		return fmt.Errorf("create_date_from must not be later than create_date_to")
	}
	for _, values := range [][]string{q.Status, q.Types, q.Suffix} {
		for _, v := range values {
			if strings.TrimSpace(v) == "" {
				return fmt.Errorf("empty filter value")
			}
		}
	}
	return nil
}

// PipelineOperationLogDAO reads the existing log table; it does not create task state.
type PipelineOperationLogDAO struct{}

func NewPipelineOperationLogDAO() *PipelineOperationLogDAO { return &PipelineOperationLogDAO{} }

func (d *PipelineOperationLogDAO) List(ctx context.Context, kbID, tenantID string, q IngestionQuery) ([]entity.PipelineOperationLog, int64, error) {
	if err := q.Validate(); err != nil {
		return nil, 0, err
	}
	query := DB.WithContext(ctx).Model(&entity.PipelineOperationLog{}).Where("kb_id = ? AND tenant_id = ?", kbID, tenantID)
	if q.LogType == "dataset" {
		query = query.Where("document_id = ?", "graph_raptor_x")
	} else {
		query = query.Where("document_id <> ?", "graph_raptor_x")
	}
	if q.Keywords != "" {
		escaped := strings.NewReplacer("\\", "\\\\", "%", "\\%", "_", "\\_").Replace(strings.ToLower(q.Keywords))
		query = query.Where("LOWER(document_name) LIKE ? ESCAPE E'\\\\'", "%"+escaped+"%")
	}
	if q.Status != nil {
		query = query.Where("operation_status IN ?", q.Status)
	}
	if q.LogType == "file" {
		if q.Types != nil {
			query = query.Where("document_type IN ?", q.Types)
		}
		if q.Suffix != nil {
			query = query.Where("document_suffix IN ?", q.Suffix)
		}
	}
	if q.From != nil {
		query = query.Where("create_date >= ?", q.From.UTC())
	}
	if q.To != nil {
		query = query.Where("create_date <= ?", q.To.UTC())
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, 0, err
	}
	direction := " ASC"
	if q.Desc {
		direction = " DESC"
	}
	query = query.Order(q.OrderBy + direction)
	if q.OrderBy != "id" {
		query = query.Order("id" + direction)
	}
	// Zero is a legal omitted pagination value; a positive size always bounds SQL.
	if q.PageSize > 0 {
		page := q.Page
		if page == 0 {
			page = 1
		}
		query = query.Offset((page - 1) * q.PageSize).Limit(q.PageSize)
	}
	logs := make([]entity.PipelineOperationLog, 0)
	if err := query.Find(&logs).Error; err != nil {
		return nil, 0, err
	}
	return logs, total, nil
}
func (d *PipelineOperationLogDAO) Get(ctx context.Context, kbID, tenantID, id string) (*entity.PipelineOperationLog, error) {
	var row entity.PipelineOperationLog
	err := DB.WithContext(ctx).Where("id = ? AND kb_id = ? AND tenant_id = ?", id, kbID, tenantID).First(&row).Error
	return &row, err
}
