//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package model

// EvaluationDataset evaluation dataset model
// Note: Python defines custom create_time/update_time (not null) instead of using BaseModel's
type EvaluationDataset struct {
	ID          string    `gorm:"column:id;primaryKey;size:32" json:"id"`
	TenantID    string    `gorm:"column:tenant_id;size:32;not null;index" json:"tenant_id"`
	Name        string    `gorm:"column:name;size:255;not null;index" json:"name"`
	Description *string   `gorm:"column:description;type:text" json:"description,omitempty"`
	KbIDs       JSONSlice `gorm:"column:kb_ids;type:jsonb;not null" json:"kb_ids"`
	CreatedBy   string    `gorm:"column:created_by;size:32;not null;index" json:"created_by"`
	// Custom time fields (not null) to match Python
	CreateTime int64 `gorm:"column:create_time;not null;index" json:"create_time"`
	UpdateTime int64 `gorm:"column:update_time;not null" json:"update_time"`
	// Python uses String(1) ("1"=valid, "0"=invalid); keep Go aligned to varchar(1) to avoid type drift
	Status string `gorm:"column:status;size:1;not null;default:1;index" json:"status"`
}

// TableName specify table name
func (EvaluationDataset) TableName() string {
	return "t_ai_evaluation_datasets"
}

// EvaluationCase evaluation case model
// Note: Python defines custom create_time (not null) instead of using BaseModel's
type EvaluationCase struct {
	ID               string     `gorm:"column:id;primaryKey;size:32" json:"id"`
	DatasetID        string     `gorm:"column:dataset_id;size:32;not null;index" json:"dataset_id"`
	Question         string     `gorm:"column:question;type:text;not null" json:"question"`
	ReferenceAnswer  *string    `gorm:"column:reference_answer;type:text" json:"reference_answer,omitempty"`
	RelevantDocIDs   *JSONSlice `gorm:"column:relevant_doc_ids;type:jsonb" json:"relevant_doc_ids,omitempty"`
	RelevantChunkIDs *JSONSlice `gorm:"column:relevant_chunk_ids;type:jsonb" json:"relevant_chunk_ids,omitempty"`
	CaseMetadata     *JSONMap   `gorm:"column:case_metadata;type:jsonb" json:"case_metadata,omitempty"`
	// Custom time field (not null) to match Python
	CreateTime int64 `gorm:"column:create_time;not null" json:"create_time"`
}

// TableName specify table name
func (EvaluationCase) TableName() string {
	return "t_ai_evaluation_cases"
}

// EvaluationRun evaluation run model
// Note: Python defines custom create_time/complete_time instead of using BaseModel's
type EvaluationRun struct {
	ID             string   `gorm:"column:id;primaryKey;size:32" json:"id"`
	DatasetID      string   `gorm:"column:dataset_id;size:32;not null;index" json:"dataset_id"`
	DialogID       string   `gorm:"column:dialog_id;size:32;not null;index" json:"dialog_id"`
	Name           string   `gorm:"column:name;size:255;not null" json:"name"`
	ConfigSnapshot JSONMap  `gorm:"column:config_snapshot;type:jsonb;not null" json:"config_snapshot"`
	MetricsSummary *JSONMap `gorm:"column:metrics_summary;type:jsonb" json:"metrics_summary,omitempty"`
	RunStatus      string   `gorm:"column:run_status;size:32;not null;default:PENDING" json:"run_status"`
	CreatedBy      string   `gorm:"column:created_by;size:32;not null;index" json:"created_by"`
	// Custom time fields to match Python
	CreateTime   int64  `gorm:"column:create_time;not null;index" json:"create_time"`
	CompleteTime *int64 `gorm:"column:complete_time" json:"complete_time,omitempty"`
}

// TableName specify table name
func (EvaluationRun) TableName() string {
	return "t_ai_evaluation_runs"
}

// EvaluationResult evaluation result model
// Note: Python defines custom create_time (not null) instead of using BaseModel's
type EvaluationResult struct {
	ID              string    `gorm:"column:id;primaryKey;size:32" json:"id"`
	RunID           string    `gorm:"column:run_id;size:32;not null;index" json:"run_id"`
	CaseID          string    `gorm:"column:case_id;size:32;not null;index" json:"case_id"`
	GeneratedAnswer string    `gorm:"column:generated_answer;type:text;not null" json:"generated_answer"`
	RetrievedChunks JSONSlice `gorm:"column:retrieved_chunks;type:jsonb;not null" json:"retrieved_chunks"`
	Metrics         JSONMap   `gorm:"column:metrics;type:jsonb;not null" json:"metrics"`
	ExecutionTime   float64   `gorm:"column:execution_time;not null" json:"execution_time"`
	TokenUsage      *JSONMap  `gorm:"column:token_usage;type:jsonb" json:"token_usage,omitempty"`
	// Custom time field to match Python
	CreateTime int64 `gorm:"column:create_time;not null" json:"create_time"`
}

// TableName specify table name
func (EvaluationResult) TableName() string {
	return "t_ai_evaluation_results"
}
