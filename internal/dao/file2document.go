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

package dao

import (
	"fmt"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/entity"
)

// File2DocumentDAO file to document mapping data access object
type File2DocumentDAO struct{}

// NewFile2DocumentDAO create file2document DAO
func NewFile2DocumentDAO() *File2DocumentDAO {
	return &File2DocumentDAO{}
}

// GetKBInfoByFileID gets knowledge base info by file ID
func (dao *File2DocumentDAO) GetKBInfoByFileID(fileID string) ([]map[string]interface{}, error) {
	var results []map[string]interface{}

	rows, err := DB.Model(&entity.File{}).
		Select("t_ai_knowledgebases.id, t_ai_knowledgebases.name, t_ai_file2documents.document_id").
		Joins("JOIN t_ai_file2documents ON t_ai_file2documents.file_id = ?", fileID).
		Joins("JOIN t_ai_documents ON t_ai_documents.id = t_ai_file2documents.document_id").
		Joins("JOIN t_ai_knowledgebases ON t_ai_knowledgebases.id = t_ai_documents.kb_id").
		Where("t_ai_files.id = ?", fileID).
		Rows()
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	for rows.Next() {
		var kbID, kbName, docID string
		if err := rows.Scan(&kbID, &kbName, &docID); err != nil {
			continue
		}
		results = append(results, map[string]interface{}{
			"kb_id":       kbID,
			"kb_name":     kbName,
			"document_id": docID,
		})
	}

	return results, nil
}

// GetByFileID gets file2document mappings by file ID
func (dao *File2DocumentDAO) GetByFileID(fileID string) ([]*entity.File2Document, error) {
	var mappings []*entity.File2Document
	err := DB.Where("file_id = ?", fileID).Find(&mappings).Error
	return mappings, err
}

// DeleteByFileID deletes file2document mappings by file ID
func (dao *File2DocumentDAO) DeleteByFileID(fileID string) error {
	return DB.Unscoped().Where("file_id = ?", fileID).Delete(&entity.File2Document{}).Error
}

// CreateInTransaction shares the document/dataset/file lock protocol with deletion.
// The caller owns tx and commits the link while both parent rows remain locked.
func (dao *File2DocumentDAO) CreateInTransaction(tx *gorm.DB, mapping *entity.File2Document) error {
	if tx == nil || mapping == nil || mapping.ID == "" || mapping.FileID == nil || *mapping.FileID == "" || mapping.DocumentID == nil || *mapping.DocumentID == "" {
		return fmt.Errorf("complete file/document mapping required")
	}
	var document entity.Document
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", *mapping.DocumentID).Take(&document).Error; err != nil {
		return err
	}
	// KB counters and deletion take the dataset lock before shared File rows.
	var dataset entity.Knowledgebase
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", document.KbID).Take(&dataset).Error; err != nil {
		return err
	}
	var file entity.File
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", *mapping.FileID).Take(&file).Error; err != nil {
		return err
	}
	return tx.Create(mapping).Error
}
