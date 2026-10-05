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

package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/tokenizer"
	"path/filepath"
	"strings"
	"time"

	"go.uber.org/zap"
)

// SkillVersionInfo represents a skill version in the file system
type SkillVersionInfo struct {
	SkillName   string   `json:"skill_name"`
	Version     string   `json:"version"`
	Description string   `json:"description"`
	Tags        []string `json:"tags"`
	Content     string   `json:"content"`
}

// FileSystemClient defines the interface for accessing skill files
type FileSystemClient interface {
	ListSkills(ctx context.Context, tenantID string) ([]SkillVersionInfo, error)
	GetSkillContent(ctx context.Context, tenantID, skillName string) (*SkillVersionInfo, error)
}

// defaultMaxLength is a safe default for embedding model max input length
const defaultMaxLength = 8191

// SkillIndexerService handles skill indexing operations
type SkillIndexerService struct {
	configDAO     *dao.SkillSearchConfigDAO
	fileDAO       *dao.FileDAO
	spaceDAO      *dao.SkillSpaceDAO
	modelProvider *SkillModelProvider
}

// NewSkillIndexerService creates a new SkillIndexerService instance
func NewSkillIndexerService() *SkillIndexerService {
	return &SkillIndexerService{
		configDAO:     dao.NewSkillSearchConfigDAO(),
		fileDAO:       dao.NewFileDAO(),
		spaceDAO:      dao.NewSkillSpaceDAO(),
		modelProvider: NewSkillModelProvider(),
	}
}

// isElasticsearch checks if the engine is Elasticsearch
func isElasticsearch(docEngine engine.SkillDocEngine) bool {
	return docEngine.GetType() == "elasticsearch"
}

// IndexSkill indexes a single skill
// Uses skill_id as doc_id for direct mapping, with version control for incremental updates
// For ES: xxx fields store original content, xxx_tks fields store RAG-tokenized content (space-separated)
// For Infinity: only xxx fields with built-in rag-analyzer
func (s *SkillIndexerService) IndexSkill(ctx context.Context, tenantID, spaceID string, skill SkillInfo, docEngine engine.SkillDocEngine, embdID string) error {
	spaceID = normalizeSpaceID(spaceID)

	config, err := s.configDAO.GetOrCreate(tenantID, spaceID, embdID)
	if err != nil {
		return fmt.Errorf("failed to get config: %w", err)
	}

	// Get field config
	fieldConfig := entity.DefaultFieldConfig()
	if config.FieldConfig != nil {
		if fcJSON, err := json.Marshal(config.FieldConfig); err == nil {
			json.Unmarshal(fcJSON, &fieldConfig)
		}
	}

	// Build vector text from enabled fields
	vectorText := BuildVectorText(skill.Name, skill.Description, skill.Tags, skill.Content, fieldConfig)

	// Generate a real embedding; failed encoding cannot publish a document.
	vector, err := s.generateEmbedding(ctx, vectorText, embdID, tenantID)
	if err != nil {
		return fmt.Errorf("embedding failed: %w", err)
	}

	// Build document with RAG tokenization for ES
	now := time.Now()
	timestamp := now.UnixMilli()

	// Get embedding dimension by calling embedding API with test text
	// This follows Python's approach: get dimension from actual embedding result
	dimension, err := s.getEmbeddingDimension(ctx, tenantID, embdID)
	if err != nil {
		return fmt.Errorf("failed to get embedding dimension: %w", err)
	}
	vectorField := fmt.Sprintf("q_%d_vec", dimension)

	// Determine engine type
	isES := isElasticsearch(docEngine)

	// Build base document
	// Use skill.Version if available, otherwise use config.IndexVersion as fallback
	skillVersion := skill.Version
	if skillVersion == "" {
		skillVersion = "1.0.0"
	}
	doc := map[string]interface{}{
		"skill_id":    skill.ID,
		"space_id":    spaceID,
		"folder_id":   skill.FolderID,
		"name":        skill.Name,
		"tags":        strings.Join(skill.Tags, ", "),
		"description": skill.Description,
		"content":     skill.Content,
		"version":     skillVersion,
		"status":      "1",
		"create_time": timestamp,
		"update_time": timestamp,
	}

	doc["_search_text"] = vectorText
	doc[vectorField] = vector

	// For ES: add tokenized fields for BM25 search
	// For Infinity: fields have built-in analyzer, no need for xxx_tks
	if isES {
		nameTokens, _ := tokenizer.Tokenize(skill.Name)
		tagsText := strings.Join(skill.Tags, " ")
		tagsTokens, _ := tokenizer.Tokenize(tagsText)

		doc["name_tks"] = nameTokens
		doc["tags_tks"] = tagsTokens

		if fieldConfig.Description.Enabled {
			descTokens, _ := tokenizer.Tokenize(skill.Description)
			doc["description_tks"] = descTokens
		}
		if fieldConfig.Content.Enabled {
			contentTokens, _ := tokenizer.Tokenize(skill.Content)
			doc["content_tks"] = contentTokens
		}
	}

	indexName := getSkillIndexName(tenantID, spaceID)

	// For Infinity: ensure table exists with correct dimension BEFORE inserting
	if docEngine.GetType() == "infinity" {
		exists, _ := docEngine.TableExists(ctx, indexName)
		if !exists {
			common.Info(fmt.Sprintf("Creating Infinity table with dimension %d", dimension))
			if err := s.createIndexWithDimension(ctx, tenantID, spaceID, docEngine, embdID, dimension); err != nil {
				return fmt.Errorf("failed to create index with dimension %d: %w", dimension, err)
			}
		}
	}

	// ES document ID cannot contain '/' - replace with '_'
	docID := strings.ReplaceAll(skill.ID, "/", "_")

	common.Info(fmt.Sprintf("Calling IndexDocument: indexName=%s, docID=%s, engineType=%s", indexName, docID, docEngine.GetType()))
	if err := docEngine.IndexDocument(ctx, indexName, docID, doc); err != nil {
		common.Error(fmt.Sprintf("IndexDocument failed: indexName=%s, docID=%s", indexName, docID), err)
		return fmt.Errorf("failed to index document: %w", err)
	}
	common.Info(fmt.Sprintf("IndexDocument succeeded: indexName=%s, docID=%s", indexName, docID))

	return nil
}

// BatchIndexSkills indexes multiple skills in batch
// Optimized to use batch embedding API for better performance
func (s *SkillIndexerService) BatchIndexSkills(ctx context.Context, tenantID, spaceID string, skills []SkillInfo, docEngine engine.SkillDocEngine, embdID string) error {
	spaceID = normalizeSpaceID(spaceID)
	if len(skills) == 0 {
		return nil
	}

	config, err := s.configDAO.GetOrCreate(tenantID, spaceID, embdID)
	if err != nil {
		return fmt.Errorf("failed to get config: %w", err)
	}

	// Get field config
	fieldConfig := entity.DefaultFieldConfig()
	if config.FieldConfig != nil {
		if fcJSON, err := json.Marshal(config.FieldConfig); err == nil {
			json.Unmarshal(fcJSON, &fieldConfig)
		}
	}

	// Build vector texts for all skills
	vectorTexts := make([]string, len(skills))
	for i, skill := range skills {
		vectorTexts[i] = BuildVectorText(skill.Name, skill.Description, skill.Tags, skill.Content, fieldConfig)
	}

	// Get embedding dimension FIRST by calling embedding API with test text
	// This follows Python's approach: must get dimension before creating table
	dimension, err := s.getEmbeddingDimension(ctx, tenantID, embdID)
	if err != nil {
		return fmt.Errorf("failed to get embedding dimension: %w", err)
	}
	common.Info(fmt.Sprintf("Using embedding dimension: %d", dimension))
	vectorField := fmt.Sprintf("q_%d_vec", dimension)

	// Generate embeddings in batch
	common.Info(fmt.Sprintf("Generating embeddings for %d skills with embdID=%s", len(skills), embdID))
	vectors, err := s.generateEmbeddings(ctx, vectorTexts, embdID, tenantID)
	if err != nil {
		return fmt.Errorf("embedding failed: %w", err)
	} else {
		common.Info(fmt.Sprintf("Generated %d vectors", len(vectors)))
	}

	// Ensure index exists with correct dimension
	indexName := getSkillIndexName(tenantID, spaceID)
	if docEngine.GetType() == "infinity" {
		// For Infinity: must ensure table exists with correct dimension BEFORE inserting
		common.Info(fmt.Sprintf("Checking if index exists: %s", indexName))
		exists, err := docEngine.TableExists(ctx, indexName)
		if err != nil {
			common.Warn(fmt.Sprintf("Error checking index existence: %v", err))
		}
		common.Info(fmt.Sprintf("Index exists: %v", exists))

		if !exists {
			// Only create if table doesn't exist
			common.Info(fmt.Sprintf("Creating index with actual dimension %d", dimension))
			if err := s.createIndexWithDimension(ctx, tenantID, spaceID, docEngine, embdID, dimension); err != nil {
				return fmt.Errorf("failed to create index with dimension %d: %w", dimension, err)
			}
			common.Info("Index created successfully")
		} else {
			common.Info("Index already exists, skipping creation")
		}
	} else {
		// For ES: just ensure index exists
		if err := s.EnsureIndex(ctx, tenantID, spaceID, docEngine, embdID); err != nil {
			return fmt.Errorf("failed to ensure index exists: %w", err)
		}
	}

	// Index all skills
	now := time.Now()
	timestamp := now.UnixMilli()
	isES := isElasticsearch(docEngine)

	var indexErrors []string
	for i, skill := range skills {

		// ES document ID cannot contain '/' - replace with '_'
		docID := strings.ReplaceAll(skill.ID, "/", "_")

		// Use skill.Version if available, otherwise default to "1.0.0"
		skillVersion := skill.Version
		if skillVersion == "" {
			skillVersion = "1.0.0"
		}

		doc := map[string]interface{}{
			"skill_id":    skill.ID,
			"space_id":    spaceID,
			"folder_id":   skill.FolderID,
			"name":        skill.Name,
			"tags":        strings.Join(skill.Tags, ", "),
			"description": skill.Description,
			"content":     skill.Content,
			"version":     skillVersion,
			"status":      "1",
			"create_time": timestamp,
			"update_time": timestamp,
		}

		doc["_search_text"] = vectorTexts[i]
		doc[vectorField] = vectors[i]

		// For ES: add tokenized fields for BM25 search
		if isES {
			nameTokens, _ := tokenizer.Tokenize(skill.Name)
			tagsText := strings.Join(skill.Tags, " ")
			tagsTokens, _ := tokenizer.Tokenize(tagsText)

			doc["name_tks"] = nameTokens
			doc["tags_tks"] = tagsTokens

			if fieldConfig.Description.Enabled {
				descTokens, _ := tokenizer.Tokenize(skill.Description)
				doc["description_tks"] = descTokens
			}
			if fieldConfig.Content.Enabled {
				contentTokens, _ := tokenizer.Tokenize(skill.Content)
				doc["content_tks"] = contentTokens
			}
		}

		common.Info("Batch: Calling IndexDocument", zap.String("indexName", indexName), zap.String("docID", docID), zap.Int("index", i))
		if err := docEngine.IndexDocument(ctx, indexName, docID, doc); err != nil {
			common.Error(fmt.Sprintf("Failed to index skill %s", skill.ID), err)
			indexErrors = append(indexErrors, fmt.Sprintf("%s: %v", skill.ID, err))
			continue
		}
	}

	if len(indexErrors) > 0 {
		return fmt.Errorf("failed to index %d skill(s): %s", len(indexErrors), strings.Join(indexErrors, "; "))
	}

	return nil
}

// DeleteSkillIndex deletes a skill's index by skill ID
// Returns nil if the document doesn't exist (idempotent delete)
func (s *SkillIndexerService) DeleteSkillIndex(ctx context.Context, tenantID, spaceID, skillID string, docEngine engine.SkillDocEngine) error {
	spaceID = normalizeSpaceID(spaceID)
	indexName := getSkillIndexName(tenantID, spaceID)
	// ES document ID cannot contain '/' - replace with '_'
	docID := strings.ReplaceAll(skillID, "/", "_")
	if err := docEngine.DeleteDocument(ctx, indexName, docID); err != nil {
		// Check if it's a "not found" error - this is OK, document might not have been indexed
		if strings.Contains(err.Error(), "not found") {
			common.Debug(fmt.Sprintf("Document %s not found in index %s, treating as already deleted", skillID, indexName))
			return nil
		}
		common.Error(fmt.Sprintf("Failed to delete document %s from index %s", skillID, indexName), err)
		return err
	}
	return nil
}

// DeleteSkillByName deletes a skill's index by skill name
// Deletes all versions: both new format (skillname) and old format (skillname_x.x.x)
func (s *SkillIndexerService) DeleteSkillByName(ctx context.Context, tenantID, spaceID, skillName string, docEngine engine.SkillDocEngine) error {
	spaceID = normalizeSpaceID(spaceID)
	indexName := getSkillIndexName(tenantID, spaceID)

	docID := strings.ReplaceAll(skillName, "/", "_")
	return docEngine.DeleteDocument(ctx, indexName, docID)
}

// UpdateSkillVersion updates a skill's index when version changes
// Prepares a replacement without removing the current document first.
func (s *SkillIndexerService) UpdateSkillVersion(ctx context.Context, tenantID, spaceID string, skill SkillInfo, docEngine engine.SkillDocEngine, embdID string) error {
	// The engine publishes the replacement only after preparing the new document.
	return s.IndexSkill(ctx, tenantID, spaceID, skill, docEngine, embdID)
}

// ReindexAll builds a complete replacement before publishing the stable index.
// A failed build leaves the prior index visible; the adapter owns physical cleanup.
func (s *SkillIndexerService) ReindexAll(ctx context.Context, tenantID, spaceID string, docEngine engine.SkillDocEngine, embdID string) (map[string]interface{}, error) {
	spaceID = normalizeSpaceID(spaceID)
	// Get current config and increment semantic version
	config, err := s.configDAO.GetOrCreate(tenantID, spaceID, embdID)
	if err != nil {
		return nil, fmt.Errorf("failed to get config: %w", err)
	}

	newVersion := incrementSemanticVersion(config.IndexVersion)
	newDimension, err := s.getEmbeddingDimension(ctx, tenantID, embdID)
	if err != nil {
		return nil, err
	}
	replacer, ok := docEngine.(engine.SkillIndexReplacer)
	if !ok {
		return nil, fmt.Errorf("ATOMIC_REINDEX_UNAVAILABLE")
	}

	// Get space info to find folder ID
	space, err := s.spaceDAO.GetByID(spaceID)
	if err != nil {
		return nil, fmt.Errorf("failed to get space: %w", err)
	}
	if space.TenantID != tenantID {
		return nil, fmt.Errorf("space not found")
	}

	spaceFolderID := space.FolderID

	// Traverse all skill folders under the space
	skills, err := s.getSkillsFromFileSystem(ctx, tenantID, spaceFolderID, spaceID)
	if err != nil {
		return nil, fmt.Errorf("failed to get skills from file system: %w", err)
	}

	common.Info(fmt.Sprintf("ReindexAll: found %d skills to index", len(skills)))

	if err := replacer.ReplaceSkillIndex(ctx, getSkillIndexName(tenantID, spaceID), newDimension, func(staged engine.SkillDocEngine) error {
		return s.BatchIndexSkills(ctx, tenantID, spaceID, skills, staged, embdID)
	}); err != nil {
		return nil, err
	}
	if err := s.configDAO.UpdateByTenantID(tenantID, spaceID, map[string]interface{}{"index_version": newVersion}); err != nil {
		return nil, err
	}

	result := map[string]interface{}{
		"indexed_count": len(skills),
		"total_skills":  len(skills),
		"version":       newVersion,
		"failed_count":  0,
	}

	return result, nil
}

// getSkillsFromFileSystem traverses the space folder and gets all skills with their latest version
func (s *SkillIndexerService) getSkillsFromFileSystem(ctx context.Context, tenantID, spaceFolderID, spaceID string) ([]SkillInfo, error) {
	var skills []SkillInfo

	// Get all skill folders under the space
	skillFolders, err := s.fileDAO.ListByParentID(spaceFolderID)
	if err != nil {
		return nil, fmt.Errorf("failed to list skill folders: %w", err)
	}

	common.Info(fmt.Sprintf("getSkillsFromFileSystem: found %d skill folders in space %s", len(skillFolders), spaceID))

	for _, skillFolder := range skillFolders {
		if skillFolder.Type != "folder" || skillFolder.TenantID != tenantID {
			continue
		}

		// Get all versions of this skill
		versions, err := s.fileDAO.ListByParentID(skillFolder.ID)
		if err != nil {
			return nil, err
		}

		// Find the latest version (highest semantic version)
		latestVersion := s.findLatestVersion(versions)
		if latestVersion == nil {
			latestVersion = skillFolder
		}

		// Get skill content from the latest version folder
		skillInfo, err := s.getSkillContentFromFolder(ctx, tenantID, skillFolder, latestVersion, spaceID)
		if errors.Is(err, errSkillMarkdownMissing) {
			continue
		}
		if err != nil {
			return nil, err
		}

		skills = append(skills, *skillInfo)
		common.Info(fmt.Sprintf("added skill %s version %s for indexing", skillFolder.Name, latestVersion.Name))
	}

	return skills, nil
}

// findLatestVersion finds the latest semantic version from a list of version folders
func (s *SkillIndexerService) findLatestVersion(versions []*entity.File) *entity.File {
	if len(versions) == 0 {
		return nil
	}

	var latest *entity.File
	latestVersionNum := []*big.Int{big.NewInt(-1), big.NewInt(-1), big.NewInt(-1)}

	for _, v := range versions {
		if v.Type != "folder" {
			continue
		}

		// Parse semantic version (e.g., "1.0.0")
		parts := strings.Split(v.Name, ".")
		if len(parts) != 3 {
			// Not a valid semver, skip
			continue
		}

		parsed := make([]*big.Int, 3)
		valid := true
		for i, part := range parts {
			if part == "" {
				valid = false
				break
			}
			for _, ch := range part {
				if ch < '0' || ch > '9' {
					valid = false
				}
			}
			parsed[i] = new(big.Int)
			if _, ok := parsed[i].SetString(part, 10); !ok {
				valid = false
			}
		}
		if !valid {
			continue
		}
		for i := 0; i < 3; i++ {
			comparison := parsed[i].Cmp(latestVersionNum[i])
			if comparison > 0 {
				latest = v
				latestVersionNum = parsed
				break
			}
			if comparison < 0 {
				break
			}
		}
	}

	return latest
}

// getSkillContentFromFolder reads skill content from the version folder
func (s *SkillIndexerService) getSkillContentFromFolder(ctx context.Context, tenantID string, skillFolder, versionFolder *entity.File, spaceID string) (*SkillInfo, error) {
	content, skillMdContent, err := s.readSkillFiles(ctx, tenantID, versionFolder.ID, "", map[string]bool{})
	if err != nil {
		return nil, err
	}
	if skillMdContent == "" {
		return nil, errSkillMarkdownMissing
	}

	// Parse SKILL.md for metadata
	name, description, tags := parseCoreMetadata(skillMdContent, skillFolder.Name)

	skillID := skillFolder.Name

	skillInfo := &SkillInfo{
		ID:          skillID,
		Name:        name,
		Description: description,
		Tags:        tags,
		Content:     content,
		FolderID:    skillFolder.ID,
		Version:     versionFolder.Name,
	}

	return skillInfo, nil
}

// isTextFileForSkill checks if a file is a text file that should be indexed
func isTextFileForSkill(fileName string) bool {
	ext := strings.ToLower(filepath.Ext(fileName))
	if ext != "" {
		ext = ext[1:] // Remove leading dot
	}

	textFileExtensions := map[string]bool{
		"md": true, "mdx": true, "txt": true, "json": true, "json5": true,
		"yaml": true, "yml": true, "toml": true, "js": true, "cjs": true, "mjs": true,
		"ts": true, "tsx": true, "jsx": true, "py": true, "sh": true, "rb": true,
		"go": true, "rs": true, "swift": true, "kt": true, "java": true, "cs": true,
		"cpp": true, "c": true, "h": true, "hpp": true, "sql": true, "csv": true,
		"ini": true, "cfg": true, "env": true, "xml": true, "html": true,
		"css": true, "scss": true, "sass": true, "svg": true,
	}

	return textFileExtensions[ext]
}

// getSpaceFolderIDByName finds the space folder ID by space name (consistent with frontend behavior)
// Frontend finds space folder by listing folders under skills folder and matching by name
func (s *SkillIndexerService) getSpaceFolderIDByName(tenantID, spaceName string) (string, error) {
	// Get root folder
	rootFolder, err := s.fileDAO.GetRootFolder(tenantID)
	if err != nil {
		return "", fmt.Errorf("failed to get root folder: %w", err)
	}

	// Find skills folder under root
	files, _, err := s.fileDAO.GetByPfID(tenantID, rootFolder.ID, 0, 0, "name", false, "")
	if err != nil {
		return "", fmt.Errorf("failed to list root folder contents: %w", err)
	}

	var skillsFolderID string
	for _, file := range files {
		if file.Type == "folder" && file.Name == "skills" {
			skillsFolderID = file.ID
			break
		}
	}

	if skillsFolderID == "" {
		return "", fmt.Errorf("skills folder not found for tenant %s", tenantID)
	}

	// Find space folder by name under skills folder
	spaceFolders, _, err := s.fileDAO.GetByPfID(tenantID, skillsFolderID, 0, 0, "name", false, "")
	if err != nil {
		return "", fmt.Errorf("failed to list skills folder contents: %w", err)
	}

	for _, folder := range spaceFolders {
		if folder.Type == "folder" && folder.Name == spaceName {
			return folder.ID, nil
		}
	}

	return "", fmt.Errorf("space folder '%s' not found under skills folder", spaceName)
}

// parseSkillMetadata parses SKILL.md content to extract metadata
func (s *SkillIndexerService) parseSkillMetadata(content, defaultName string) (name, description string, tags []string) {
	name = defaultName

	if content == "" {
		return name, "", nil
	}

	// Parse YAML frontmatter
	lines := strings.Split(content, "\n")
	if len(lines) == 0 || strings.TrimSpace(lines[0]) != "---" {
		return name, "", nil
	}

	var endIndex int
	found := false
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			endIndex = i
			found = true
			break
		}
	}

	if !found {
		return name, "", nil
	}

	// Parse frontmatter lines
	for i := 1; i < endIndex; i++ {
		line := lines[i]
		if strings.HasPrefix(line, "name:") {
			name = strings.TrimSpace(strings.TrimPrefix(line, "name:"))
		} else if strings.HasPrefix(line, "description:") {
			description = strings.TrimSpace(strings.TrimPrefix(line, "description:"))
		} else if strings.HasPrefix(line, "tags:") {
			// Parse tags array
			tagsLine := strings.TrimSpace(strings.TrimPrefix(line, "tags:"))
			if strings.HasPrefix(tagsLine, "[") && strings.HasSuffix(tagsLine, "]") {
				// Array format: [tag1, tag2]
				tagsStr := strings.Trim(tagsLine, "[]")
				tags = strings.Split(tagsStr, ",")
				for i, tag := range tags {
					tags[i] = strings.TrimSpace(tag)
				}
			} else if tagsLine != "" {
				// Single tag or dash list
				tags = []string{tagsLine}
			}
		}
	}

	return name, description, tags
}

// getFileContent retrieves the content of a file from storage
func (s *SkillIndexerService) getFileContent(ctx context.Context, tenantID string, file *entity.File) ([]byte, error) {
	if file.Location == nil || *file.Location == "" {
		return nil, fmt.Errorf("file location is empty")
	}

	_, blobs := skillCoreResources()
	if blobs == nil {
		return nil, fmt.Errorf("storage unavailable")
	}
	content, err := blobs.Get(file.ParentID, *file.Location)
	if err != nil {
		return nil, err
	}

	return content, nil
}

// incrementSemanticVersion increments the patch version of a semantic version string
// Supports format: "major.minor.patch" (e.g., "1.0.0" -> "1.0.1")
// If version is empty or invalid, returns "1.0.0"
func incrementSemanticVersion(version string) string {
	if version == "" {
		return "1.0.0"
	}

	parts := strings.Split(version, ".")
	if len(parts) != 3 {
		// Invalid format, reset to 1.0.0
		return "1.0.0"
	}

	// Try to parse patch version
	var major, minor, patch int
	fmt.Sscanf(parts[0], "%d", &major)
	fmt.Sscanf(parts[1], "%d", &minor)
	fmt.Sscanf(parts[2], "%d", &patch)

	// Increment patch version
	patch++
	if patch > 999 {
		patch = 0
		minor++
		if minor > 999 {
			minor = 0
			major++
		}
	}

	return fmt.Sprintf("%d.%d.%d", major, minor, patch)
}

// cleanupOldVersions removes documents with version less than current version
func (s *SkillIndexerService) cleanupOldVersions(ctx context.Context, tenantID, spaceID string, currentVersion string, docEngine engine.SkillDocEngine) error {
	// This is a placeholder - actual implementation would:
	// 1. Search for documents where version < currentVersion (semantic version comparison)
	// 2. Delete those documents
	// The actual implementation depends on the search engine's query capabilities
	// For now, we rely on the fact that skill_id is used as doc_id,
	// so re-indexing the same skill_id will overwrite the document
	return nil
}

// InitializeIndex initializes the skill search index for a tenant
func (s *SkillIndexerService) InitializeIndex(ctx context.Context, tenantID, spaceID string, docEngine engine.SkillDocEngine, embdID string) error {
	// Check if index exists
	indexName := getSkillIndexName(tenantID, spaceID)

	common.Info("Checking skill index existence", zap.String("indexName", indexName), zap.String("tenantID", tenantID), zap.String("spaceID", spaceID))

	exists, err := docEngine.TableExists(ctx, indexName)
	if err != nil {
		common.Error("Failed to check index existence", err)
		return fmt.Errorf("failed to check index existence: %w", err)
	}

	if !exists {
		common.Info("Skill index does not exist, creating...", zap.String("indexName", indexName))
		return s.createIndex(ctx, tenantID, spaceID, docEngine, embdID)
	}

	common.Info("Skill search index already exists", zap.String("indexName", indexName))
	return nil
}

// createIndex creates the skill index using mapping files
func (s *SkillIndexerService) createIndex(ctx context.Context, tenantID, spaceID string, docEngine engine.SkillDocEngine, embdID string) error {
	// Get embedding dimension by calling embedding API with test text
	dimension, err := s.getEmbeddingDimension(ctx, tenantID, embdID)
	if err != nil {
		return fmt.Errorf("failed to get embedding dimension: %w", err)
	}
	return s.createIndexWithDimension(ctx, tenantID, spaceID, docEngine, embdID, dimension)
}

// createIndexWithDimension creates the skill index with a specific vector dimension
func (s *SkillIndexerService) createIndexWithDimension(ctx context.Context, tenantID, spaceID string, docEngine engine.SkillDocEngine, embdID string, dimension int) error {
	indexName := getSkillIndexName(tenantID, spaceID)

	common.Info(fmt.Sprintf("Creating skill index with dimension %d", dimension),
		zap.String("indexName", indexName),
		zap.String("spaceID", spaceID),
		zap.Int("dimension", dimension),
		zap.String("engineType", docEngine.GetType()))

	// Use the doc engine's CreateDataset method with skill-specific mapping
	// The mapping file is loaded from conf/skill_es_mapping.json or conf/skill_infinity_mapping.json
	err := docEngine.CreateDataset(ctx, indexName, "skill", dimension, "")
	if err != nil {
		common.Error("Failed to create skill index", err)
		return err
	}
	common.Info("Successfully created skill index", zap.String("indexName", indexName))
	return nil
}

// EnsureIndex ensures the skill index exists for a tenant
func (s *SkillIndexerService) EnsureIndex(ctx context.Context, tenantID, spaceID string, docEngine engine.SkillDocEngine, embdID string) error {
	return s.InitializeIndex(ctx, tenantID, spaceID, docEngine, embdID)
}

// generateEmbedding generates embedding for text using the specified model
func (s *SkillIndexerService) generateEmbedding(ctx context.Context, text, embdID, tenantID string) ([]float64, error) {
	if s.modelProvider == nil {
		return nil, fmt.Errorf("model provider not set")
	}

	if embdID == "" {
		return nil, fmt.Errorf("embedding model ID not configured")
	}

	embeddingModel, err := s.modelProvider.GetEmbeddingModel(tenantID, embdID)
	if err != nil {
		return nil, fmt.Errorf("failed to get embedding model: %w", err)
	}

	// Truncate text to prevent exceeding model's max input length
	maxLen := embeddingModel.MaxTokens
	if maxLen <= 0 {
		maxLen = defaultMaxLength
	}
	truncatedText := truncate(text, maxLen-10)

	vectors, err := embeddingModel.Encode(ctx, []string{truncatedText}, false)
	if err != nil {
		return nil, fmt.Errorf("failed to encode text: %w", err)
	}
	if len(vectors) == 0 {
		return nil, fmt.Errorf("embedding returned empty result")
	}

	return vectors[0], nil
}

// generateEmbeddings generates embeddings for multiple texts in batch
// This is more efficient than calling generateEmbedding individually
func (s *SkillIndexerService) generateEmbeddings(ctx context.Context, texts []string, embdID, tenantID string) ([][]float64, error) {
	common.Info(fmt.Sprintf("generateEmbeddings called: texts=%d, embdID=%s, tenantID=%s", len(texts), embdID, tenantID))

	if s.modelProvider == nil {
		return nil, fmt.Errorf("model provider not set")
	}

	if embdID == "" {
		return nil, fmt.Errorf("embedding model ID not configured")
	}

	common.Info(fmt.Sprintf("Getting embedding model for %s", embdID))
	embeddingModel, err := s.modelProvider.GetEmbeddingModel(tenantID, embdID)
	if err != nil {
		common.Error(fmt.Sprintf("Failed to get embedding model: %v", err), err)
		return nil, fmt.Errorf("failed to get embedding model: %w", err)
	}

	// Truncate texts to prevent exceeding model's max input length
	maxLen := embeddingModel.MaxTokens
	if maxLen <= 0 {
		maxLen = defaultMaxLength
	}
	truncatedTexts := make([]string, len(texts))
	for i, text := range texts {
		truncatedTexts[i] = truncate(text, maxLen-10)
	}

	common.Info(fmt.Sprintf("Encoding %d texts", len(truncatedTexts)))
	// Use batch encode API (consistent with Python's encode(texts: list))
	vectors, err := embeddingModel.Encode(ctx, truncatedTexts, false)
	if err != nil {
		common.Error(fmt.Sprintf("Failed to encode texts: %v", err), err)
		return nil, fmt.Errorf("failed to encode texts: %w", err)
	}

	common.Info(fmt.Sprintf("Encoded successfully, got %d vectors", len(vectors)))
	if len(vectors) > 0 {
		common.Info(fmt.Sprintf("Vector dimension: %d", len(vectors[0])))
	}

	return vectors, nil
}

// truncate truncates text to maxLen characters
// Similar to Python's truncate function in rag/llm/embedding_model.py
func truncate(text string, maxLen int) string {
	if maxLen <= 0 {
		return text
	}
	runes := []rune(text)
	if len(runes) <= maxLen {
		return text
	}
	return string(runes[:maxLen])
}

// getEmbeddingDimension gets the embedding dimension by calling the embedding API with test text
// This follows Python's approach: use actual embedding result to determine dimension
// If embedding API fails, returns error (cannot create table without knowing dimension)
func (s *SkillIndexerService) getEmbeddingDimension(ctx context.Context, tenantID, embdID string) (int, error) {
	if s.modelProvider == nil {
		return 0, fmt.Errorf("model provider not set")
	}

	if embdID == "" {
		return 0, fmt.Errorf("embedding model ID not configured")
	}

	embeddingModel, err := s.modelProvider.GetEmbeddingModel(tenantID, embdID)
	if err != nil {
		return 0, fmt.Errorf("failed to get embedding model: %w", err)
	}

	// Use simple test text like Python does: embedding_model.encode(["ok"])
	testText := "ok"
	vectors, err := embeddingModel.Encode(ctx, []string{testText}, false)
	if err != nil {
		return 0, fmt.Errorf("failed to encode test text: %w", err)
	}

	if len(vectors) == 0 || len(vectors[0]) == 0 {
		return 0, fmt.Errorf("embedding returned empty vector")
	}

	dimension := len(vectors[0])
	common.Info(fmt.Sprintf("Got embedding dimension from API: %d", dimension))
	return dimension, nil
}
