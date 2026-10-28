package service

import (
	"context"
	"errors"
	"gorm.io/gorm"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"time"
)

func (s *DatasetsService) ingestionScope(ctx context.Context, datasetID, userID string) (*entity.Knowledgebase, error) {
	kb, err := s.kbDAO.GetReadableByID(ctx, datasetID, userID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrDatasetAccessDenied
	}
	return kb, err
}
func (s *DatasetsService) GetIngestionSummary(ctx context.Context, datasetID, userID string) (map[string]any, error) {
	kb, err := s.ingestionScope(ctx, datasetID, userID)
	if err != nil {
		return nil, err
	}
	status, err := s.documentDAO.GetParsingStatusByKBID(ctx, datasetID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"doc_num": kb.DocNum, "chunk_num": kb.ChunkNum, "token_num": kb.TokenNum, "status": status}, nil
}
func (s *DatasetsService) ListIngestionLogs(ctx context.Context, datasetID, userID string, q dao.IngestionQuery) (map[string]any, error) {
	if err := q.Validate(); err != nil {
		return nil, err
	}
	kb, err := s.ingestionScope(ctx, datasetID, userID)
	if err != nil {
		return nil, err
	}
	rows, total, err := dao.NewPipelineOperationLogDAO().List(ctx, datasetID, kb.TenantID, q)
	if err != nil {
		return nil, err
	}
	items := make([]map[string]any, 0, len(rows))
	for i := range rows {
		items = append(items, ingestionLogMap(&rows[i], q.LogType == "file"))
	}
	return map[string]any{"logs": items, "total": total}, nil
}
func (s *DatasetsService) GetIngestionLog(ctx context.Context, datasetID, userID, id string) (map[string]any, error) {
	kb, err := s.ingestionScope(ctx, datasetID, userID)
	if err != nil {
		return nil, err
	}
	row, err := dao.NewPipelineOperationLogDAO().Get(ctx, datasetID, kb.TenantID, id)
	if err != nil {
		return nil, err
	}
	return ingestionLogMap(row, true), nil
}
func ingestionTime(t *time.Time) any {
	if t == nil {
		return nil
	}
	return t.Format("2006-01-02 15:04:05")
}
func ingestionLogMap(row *entity.PipelineOperationLog, full bool) map[string]any {
	result := map[string]any{"id": row.ID, "tenant_id": row.TenantID, "kb_id": row.KbID, "progress": row.Progress, "progress_msg": row.ProgressMsg, "process_begin_at": ingestionTime(row.ProcessBeginAt), "process_duration": row.ProcessDuration, "task_type": row.TaskType, "operation_status": row.OperationStatus, "avatar": row.Avatar, "status": row.Status, "create_time": row.CreateTime, "create_date": ingestionTime(row.CreateDate), "update_time": row.UpdateTime, "update_date": ingestionTime(row.UpdateDate)}
	if full {
		for key, value := range map[string]any{"document_id": row.DocumentID, "pipeline_id": row.PipelineID, "pipeline_title": row.PipelineTitle, "parser_id": row.ParserID, "document_name": row.DocumentName, "document_suffix": row.DocumentSuffix, "document_type": row.DocumentType, "source_from": row.SourceFrom, "dsl": row.DSL} {
			result[key] = value
		}
	}
	return result
}
