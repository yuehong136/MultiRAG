package service

import (
	"multirag/internal/entity"
	"strings"
	"time"
)

func mapDocumentListItem(doc *entity.DocumentListItem) map[string]any {
	config := doc.ParserConfig
	if config == nil {
		config = entity.JSONMap{}
	}
	row := map[string]any{
		"id": doc.ID, "dataset_id": doc.KbID, "name": documentStringValue(doc.Name), "thumbnail": documentStringValue(doc.Thumbnail),
		"size": doc.Size, "type": doc.Type, "created_by": doc.CreatedBy, "location": documentStringValue(doc.Location),
		"token_count": doc.TokenNum, "chunk_count": doc.ChunkNum, "progress": doc.Progress, "progress_msg": documentStringValue(doc.ProgressMsg),
		"process_begin_at": documentTimeValue(doc.ProcessBeginAt), "process_duration": doc.ProcessDuration, "suffix": doc.Suffix,
		"run": mapDocumentRunStatus(doc.Run), "status": documentStringValue(doc.Status), "chunk_method": doc.ParserID, "parser_id": doc.ParserID,
		"pipeline_id": documentStringValue(doc.PipelineID), "pipeline_name": documentStringValue(doc.PipelineName), "nickname": documentStringValue(doc.Nickname),
		"parser_config": config, "create_time": int64(0), "create_date": documentTimeValue(doc.CreateDate), "update_time": int64(0), "update_date": documentTimeValue(doc.UpdateDate),
	}
	if doc.CreateTime != nil {
		row["create_time"] = *doc.CreateTime
	}
	if doc.UpdateTime != nil {
		row["update_time"] = *doc.UpdateTime
	}
	return row
}

func documentStringValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
func documentTimeValue(value *time.Time) string {
	if value == nil {
		return ""
	}
	return value.Format("2006-01-02 15:04:05")
}
func mapDocumentRunStatus(value *string) string {
	if value == nil {
		return "UNSTART"
	}
	status := strings.TrimSpace(*value)
	switch status {
	case "0":
		return "UNSTART"
	case "1":
		return "RUNNING"
	case "2":
		return "CANCEL"
	case "3":
		return "DONE"
	case "4":
		return "FAIL"
	}
	return status
}
