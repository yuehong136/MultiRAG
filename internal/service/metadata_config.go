package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"math"
	"strconv"
	"strings"
	"unicode/utf8"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

type MetadataConfigRequest struct {
	Metadata        json.RawMessage `json:"metadata"`
	BuiltInMetadata json.RawMessage `json:"built_in_metadata"`
	Enabled         *bool           `json:"enabled"`
}

func validateMetadataFields(raw json.RawMessage, schemaAllowed bool) (any, error) {
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if decoder.Decode(&value) != nil || value == nil {
		return nil, errors.New("Invalid metadata configuration.")
	}
	if schema, ok := value.(map[string]any); ok && schemaAllowed {
		if len(schema) != 0 {
			kind, present := schema["type"]
			_, properties := schema["properties"].(map[string]any)
			if present && kind != "object" || !properties {
				return nil, errors.New("Metadata schema requires object properties.")
			}
		}
		return schema, nil
	}
	fields, ok := value.([]any)
	if !ok {
		return nil, errors.New("Metadata fields must be an array.")
	}
	for _, item := range fields {
		field, ok := item.(map[string]any)
		if !ok {
			return nil, errors.New("Invalid metadata field.")
		}
		identity := ""
		for _, name := range []string{"key", "name"} {
			if v, present := field[name]; present && v != nil {
				text, ok := v.(string)
				if !ok || strings.TrimSpace(text) == "" || utf8.RuneCountInString(strings.TrimSpace(text)) > 255 {
					return nil, errors.New("Invalid metadata key.")
				}
				text = strings.TrimSpace(text)
				if identity != "" && identity != text {
					return nil, errors.New("Metadata key and name conflict.")
				}
				identity = text
				field[name] = text
			}
		}
		if identity == "" {
			return nil, errors.New("Metadata key is required.")
		}
		kind := ""
		if v := field["type"]; v != nil {
			var ok bool
			kind, ok = v.(string)
			if !ok || (kind != "string" && kind != "list" && kind != "time" && kind != "number") {
				return nil, errors.New("Invalid metadata type.")
			}
		}
		if v := field["description"]; v != nil {
			if _, ok := v.(string); !ok {
				return nil, errors.New("Invalid metadata description.")
			}
		}
		restrict := false
		if v, present := field["restrict_values"]; present {
			var ok bool
			restrict, ok = v.(bool)
			if !ok {
				return nil, errors.New("Invalid metadata restriction.")
			}
		}
		if kind == "list" {
			if v, present := field["items"]; present {
				switch v.(type) {
				case map[string]any, bool:
				default:
					return nil, errors.New("Invalid list item schema.")
				}
			}
		}
		for _, key := range []string{"enum", "examples"} {
			if v := field[key]; v != nil {
				values, ok := v.([]any)
				if !ok {
					return nil, errors.New("Invalid metadata values.")
				}
				for _, v := range values {
					if kind == "number" {
						var number float64
						switch v := v.(type) {
						case float64:
							number = v
						case json.Number:
							var err error
							number, err = strconv.ParseFloat(v.String(), 64)
							if err != nil {
								return nil, errors.New("Metadata values must be finite numbers.")
							}
						case string:
							var err error
							number, err = strconv.ParseFloat(strings.TrimSpace(v), 64)
							if err != nil {
								return nil, errors.New("Metadata values must be finite numbers.")
							}
						default:
							return nil, errors.New("Metadata values must be finite numbers.")
						}
						if math.IsNaN(number) || math.IsInf(number, 0) {
							return nil, errors.New("Metadata values must be finite numbers.")
						}
					} else if _, ok := v.(string); !ok {
						return nil, errors.New("Metadata values must be strings.")
					}
				}
			}
		}
		if restrict && field["enum"] != nil && field["examples"] != nil {
			a, _ := json.Marshal(field["enum"])
			b, _ := json.Marshal(field["examples"])
			if string(a) != string(b) {
				return nil, errors.New("Metadata values conflict.")
			}
		}
	}
	return fields, nil
}

func metadataConfigView(config entity.JSONMap) map[string]any {
	result := map[string]any{"metadata": []any{}, "built_in_metadata": []any{}, "enabled": false}
	for _, key := range []string{"metadata", "built_in_metadata"} {
		if config[key] != nil {
			result[key] = config[key]
		}
	}
	if v, ok := config["enable_metadata"].(bool); ok {
		result["enabled"] = v
	}
	// Match the production read projection while retaining extension attributes.
	if fields, ok := result["metadata"].([]any); ok {
		for _, item := range fields {
			if field, ok := item.(map[string]any); ok {
				if _, present := field["key"]; !present && field["name"] != nil {
					field["key"] = field["name"]
					delete(field, "name")
				}
				if field["restrict_values"] == true && field["examples"] != nil && field["enum"] == nil {
					field["enum"] = field["examples"]
				}
			}
		}
	}
	return result
}

// readMetadataParserConfig retains JSON numbers while merging unrelated parser settings.
func readMetadataParserConfig(tx *gorm.DB, id string) (entity.JSONMap, error) {
	var raw []byte
	if err := tx.Model(&entity.Knowledgebase{}).Select("parser_config").Where("id = ?", id).Row().Scan(&raw); err != nil {
		return nil, err
	}
	config := entity.JSONMap{}
	if len(raw) == 0 || string(raw) == "null" {
		return config, nil
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&config); err != nil {
		return nil, err
	}
	return config, nil
}

func (s *DatasetsService) GetMetadataConfig(ctx context.Context, id, userID string) (map[string]any, common.ErrorCode, error) {
	id, err := normalizeDatasetUUID1(strings.TrimSpace(id))
	if err != nil {
		return nil, common.CodeArgumentError, errors.New("Invalid dataset ID.")
	}
	kb, err := s.kbDAO.GetReadableByID(ctx, id, userID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeForbidden, ErrDatasetAccessDenied
	}
	if err != nil {
		return nil, common.CodeServerError, errors.New("Unable to read metadata configuration.")
	}
	var active int64
	if err := dao.DB.WithContext(ctx).Model(&entity.Tenant{}).Where("id = ? AND status = ?", kb.TenantID, "1").Count(&active).Error; err != nil {
		return nil, common.CodeServerError, errors.New("Unable to read metadata configuration.")
	}
	if active != 1 {
		return nil, common.CodeForbidden, ErrDatasetAccessDenied
	}
	config, err := readMetadataParserConfig(dao.DB.WithContext(ctx), kb.ID)
	if err != nil {
		return nil, common.CodeServerError, errors.New("Unable to read metadata configuration.")
	}
	return metadataConfigView(config), common.CodeSuccess, nil
}

func (s *DatasetsService) UpdateMetadataConfig(ctx context.Context, id, userID string, req MetadataConfigRequest) (map[string]any, common.ErrorCode, error) {
	id, err := normalizeDatasetUUID1(strings.TrimSpace(id))
	if err != nil {
		return nil, common.CodeArgumentError, errors.New("Invalid dataset ID.")
	}
	updates := entity.JSONMap{}
	for key, raw := range map[string]json.RawMessage{"metadata": req.Metadata, "built_in_metadata": req.BuiltInMetadata} {
		if raw != nil {
			value, err := validateMetadataFields(raw, key == "metadata")
			if err != nil {
				return nil, common.CodeArgumentError, err
			}
			updates[key] = value
		}
	}
	if req.Enabled != nil {
		updates["enable_metadata"] = *req.Enabled
	}
	var result map[string]any
	err = dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var kb entity.Knowledgebase
		err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ?", id, "1").
			Where("EXISTS (SELECT 1 FROM t_ai_user_tenants m WHERE m.tenant_id = t_ai_knowledgebases.tenant_id AND m.user_id = ? AND m.status = ? AND m.role IN ('owner','admin','normal'))", userID, "1").
			Where("EXISTS (SELECT 1 FROM t_ai_tenants t WHERE t.id = t_ai_knowledgebases.tenant_id AND t.status = ?)", "1").
			Where("created_by = ? OR EXISTS (SELECT 1 FROM t_ai_user_tenants m WHERE m.tenant_id = t_ai_knowledgebases.tenant_id AND m.user_id = ? AND m.status = ? AND m.role IN ('owner','admin'))", userID, userID, "1").Take(&kb).Error
		if err != nil {
			return err
		}
		kb.ParserConfig, err = readMetadataParserConfig(tx, kb.ID)
		if err != nil {
			return err
		}
		for key, value := range updates {
			kb.ParserConfig[key] = value
		}
		if len(updates) > 0 {
			if err := tx.Model(&kb).Update("parser_config", kb.ParserConfig).Error; err != nil {
				return err
			}
		}
		result = metadataConfigView(kb.ParserConfig)
		return nil
	})
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeForbidden, ErrDatasetAccessDenied
	}
	if err != nil {
		return nil, common.CodeServerError, errors.New("Unable to update metadata configuration.")
	}
	return result, common.CodeSuccess, nil
}
