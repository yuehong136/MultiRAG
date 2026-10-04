package service

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

var errSkillMarkdownMissing = errors.New("SKILL.md missing")

// ResolveSkills reads authoritative persisted content, after checking every
// folder's tenant/space ownership. Client content never escapes these checks.
func (s *SkillIndexerService) ResolveSkills(ctx context.Context, tenant, spaceID string, requested []SkillInfo) ([]SkillInfo, error) {
	space, e := s.spaceDAO.GetByID(spaceID)
	if e != nil || space.TenantID != tenant {
		return nil, fmt.Errorf("space not found")
	}
	result := []SkillInfo{}
	for _, want := range requested {
		var folder entity.File
		if e := dao.DB.Where("id = ? AND tenant_id = ? AND parent_id = ? AND type = 'folder'", want.FolderID, tenant, space.FolderID).First(&folder).Error; e != nil {
			return nil, fmt.Errorf("skill folder not found")
		}
		children, e := s.fileDAO.ListByParentID(folder.ID)
		if e != nil {
			return nil, e
		}
		version := s.findLatestVersion(children)
		if want.Version != "" {
			version = nil
			for _, child := range children {
				if child.TenantID == tenant && child.Name == want.Version && child.Type == "folder" {
					version = child
					break
				}
			}
			if version == nil {
				return nil, fmt.Errorf("version folder not found")
			}
		}
		if version == nil {
			version = &folder
		}
		info, e := s.getSkillContentFromFolder(ctx, tenant, &folder, version, spaceID)
		if e != nil {
			return nil, e
		}
		result = append(result, *info)
	}
	return result, nil
}
func (s *SkillIndexerService) readSkillFiles(ctx context.Context, tenant, folderID, prefix string, seen map[string]bool) (string, string, error) {
	if seen[folderID] {
		return "", "", fmt.Errorf("file tree cycle")
	}
	seen[folderID] = true
	var files []entity.File
	if e := dao.DB.WithContext(ctx).Where("parent_id = ? AND tenant_id = ? AND id <> parent_id", folderID, tenant).Order("name COLLATE \"C\",id").Find(&files).Error; e != nil {
		return "", "", e
	}
	var content strings.Builder
	markdown := ""
	for _, file := range files {
		path := prefix + file.Name
		if file.Type == "folder" {
			sub, _, e := s.readSkillFiles(ctx, tenant, file.ID, path+"/", seen)
			if e != nil {
				return "", "", e
			}
			content.WriteString(sub)
			continue
		}
		if !isTextFileForSkill(file.Name) {
			continue
		}
		data, e := s.getFileContent(ctx, tenant, &file)
		if e != nil {
			return "", "", e
		}
		if prefix == "" && strings.EqualFold(file.Name, "SKILL.md") {
			markdown = string(data)
		}
		content.WriteString("\n=== " + path + " ===\n")
		content.Write(data)
	}
	return content.String(), markdown, nil
}
func parseCoreMetadata(content, defaultName string) (string, string, []string) {
	name := defaultName
	parts := strings.SplitN(strings.ReplaceAll(content, "\r\n", "\n"), "\n", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[0]) != "---" {
		return name, "", nil
	}
	end := strings.Index("\n"+parts[1], "\n---\n")
	if end < 0 {
		return name, "", nil
	}
	var metadata struct {
		Name        string   `yaml:"name"`
		Description string   `yaml:"description"`
		Tags        []string `yaml:"tags"`
	}
	if yaml.Unmarshal([]byte(parts[1][:end]), &metadata) != nil {
		return name, "", nil
	}
	if metadata.Name != "" {
		name = metadata.Name
	}
	return name, metadata.Description, metadata.Tags
}
func ValidateSkillUploadPath(name string) error {
	if name == "" || strings.HasPrefix(name, "/") || strings.ContainsAny(name, "\\\x00") {
		return fmt.Errorf("INVALID_PATH")
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." || utf8.RuneCountInString(part) > 255 {
			return fmt.Errorf("INVALID_PATH")
		}
	}
	return nil
}
