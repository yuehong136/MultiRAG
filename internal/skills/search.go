package skills

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode/utf8"

	"gorm.io/gorm"
	"multirag/internal/entity"
)

func asJSON(v any) JSON {
	switch x := v.(type) {
	case JSON:
		return x
	case map[string]any:
		return JSON(x)
	}
	return nil
}
func configSnapshot(g Generation) (Config, error) {
	var c Config
	var fields JSON
	raw, e := json.Marshal(g.Config)
	if e != nil {
		return c, e
	}
	if e = json.Unmarshal(raw, &fields); e != nil {
		return c, e
	}
	c.Fields = asJSON(fields["fields"])
	c.EmbeddingModelID, e = parseModelID(fields["embedding_model_id"])
	if e != nil {
		return c, e
	}
	c.RerankModelID, e = parseModelID(fields["rerank_model_id"])
	if e != nil {
		return c, e
	}
	n, _ := number(fields["top_k"])
	c.TopK = int(n)
	c.VectorWeight, _ = number(fields["vector_weight"])
	c.SimilarityThreshold, _ = number(fields["similarity_threshold"])
	return c, nil
}
func splitText(text string, maxTokens int64) ([]string, error) {
	if maxTokens < 1 {
		return nil, fault(422, "MODEL_LIMIT_INVALID")
	}
	budget := int(min(int64(8192), max(int64(1), maxTokens*8/10)))
	out := []string{}
	start, used := 0, 0
	for i, r := range text {
		size := utf8.RuneLen(r)
		if size > budget {
			return nil, fault(422, "MODEL_LIMIT_INVALID")
		}
		if used+size > budget {
			out = append(out, text[start:i])
			start = i
			used = 0
		}
		used += size
	}
	if start < len(text) {
		out = append(out, text[start:])
	}
	return out, nil
}
func vectors32(vectors [][]float64, count int) ([][]float32, int, error) {
	if len(vectors) != count || count == 0 {
		return nil, 0, fault(503, "EMBEDDING_INVALID")
	}
	dim := len(vectors[0])
	if dim < 1 || dim > 32768 {
		return nil, 0, fault(503, "EMBEDDING_INVALID")
	}
	out := make([][]float32, count)
	for i, v := range vectors {
		if len(v) != dim {
			return nil, 0, fault(503, "EMBEDDING_INVALID")
		}
		out[i] = make([]float32, dim)
		nonzero := false
		for j, n := range v {
			if math.IsNaN(n) || math.IsInf(n, 0) || math.Abs(n) > math.MaxFloat32 {
				return nil, 0, fault(503, "EMBEDDING_INVALID")
			}
			out[i][j] = float32(n)
			nonzero = nonzero || out[i][j] != 0
		}
		if !nonzero {
			return nil, 0, fault(503, "EMBEDDING_INVALID")
		}
	}
	return out, dim, nil
}
func (s *Service) indexRows(ctx context.Context, tenant string, cfg Config, skills []Skill, versions []Version) ([]IndexRow, int, error) {
	if cfg.EmbeddingModelID == nil || s.Models == nil {
		return nil, 0, fault(503, "MODEL_UNAVAILABLE")
	}
	var model entity.TenantLLM
	if e := s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", *cfg.EmbeddingModelID, tenant).First(&model).Error; e != nil {
		return nil, 0, e
	}
	bySkill := map[string]Skill{}
	for _, sk := range skills {
		bySkill[sk.ID] = sk
	}
	rows := []IndexRow{}
	for _, v := range versions {
		sk := bySkill[v.SkillID]
		var bindings []VersionFile
		if e := s.DB.WithContext(ctx).Where("tenant_id = ? AND version_id = ?", tenant, v.ID).Order("relative_path").Find(&bindings).Error; e != nil {
			return nil, 0, e
		}
		sort.Slice(bindings, func(i, j int) bool { return bindings[i].RelativePath < bindings[j].RelativePath })
		if e := verifyManifest(v, bindings); e != nil {
			return nil, 0, e
		}
		content := []string{}
		var description string
		var tags []string
		for _, b := range bindings {
			data, e := s.readFile(ctx, tenant, b)
			if e != nil {
				return nil, 0, e
			}
			if utf8.Valid(data) {
				content = append(content, "\n=== "+b.RelativePath+" ===\n"+string(data))
			}
			if b.RelativePath == "SKILL.md" {
				description, tags, e = metadata(data, sk.Name)
				if e != nil {
					return nil, 0, e
				}
			}
		}
		values := map[string]string{"name": sk.Name, "description": description, "tags": strings.Join(tags, " "), "content": strings.Join(content, "")}
		for _, field := range []string{"name", "tags", "description", "content"} {
			f := asJSON(cfg.Fields[field])
			enabled, _ := f["enabled"].(bool)
			w, _ := number(f["weight"])
			if !enabled || w <= 0 {
				continue
			}
			chunks, e := splitText(values[field], model.MaxTokens)
			if e != nil {
				return nil, 0, e
			}
			for i, text := range chunks {
				sum := sha256.Sum256([]byte(fmt.Sprintf("%s\x00%s\x00%d", v.ID, field, i)))
				rows = append(rows, IndexRow{ID: hex.EncodeToString(sum[:])[:32], SkillID: sk.ID, VersionID: v.ID, Field: field, Text: text, Digest: v.ContentDigest})
			}
		}
	}
	dim := 0
	for start := 0; start < len(rows); start += 32 {
		end := min(start+32, len(rows))
		texts := []string{}
		for _, r := range rows[start:end] {
			texts = append(texts, r.Text)
		}
		vectors, e := s.Models.Embed(ctx, tenant, *cfg.EmbeddingModelID, texts, false)
		if e != nil {
			return nil, 0, e
		}
		converted, d, e := vectors32(vectors, len(texts))
		if e != nil {
			return nil, 0, e
		}
		if dim != 0 && dim != d {
			return nil, 0, fault(503, "EMBEDDING_INVALID")
		}
		dim = d
		for i, v := range converted {
			rows[start+i].Vector = v
		}
	}
	if len(rows) == 0 {
		vectors, e := s.Models.Embed(ctx, tenant, *cfg.EmbeddingModelID, []string{"skills"}, false)
		if e != nil {
			return nil, 0, e
		}
		_, dim, e = vectors32(vectors, 1)
		if e != nil {
			return nil, 0, e
		}
	}
	return rows, dim, nil
}
func (s *Service) Search(ctx context.Context, tenant, space, query, mode string, page, size int) (JSON, error) {
	sp, e := s.Space(ctx, tenant, space)
	if e != nil {
		return nil, e
	}
	if mode != "keyword" && mode != "vector" && mode != "hybrid" {
		return nil, fault(422, "INVALID_SEARCH_MODE")
	}
	if page < 1 || size < 1 || size > 100 {
		return nil, fault(422, "INVALID_PAGINATION")
	}
	q := s.DB.WithContext(ctx).Model(&Skill{}).Joins("JOIN t_ai_skill_versions v ON v.id = t_ai_skills.active_version_id AND v.state = 'installed'").Where("t_ai_skills.tenant_id = ? AND t_ai_skills.space_id = ? AND t_ai_skills.state = 'active'", tenant, space)
	if strings.TrimSpace(query) == "" {
		if mode != "keyword" {
			return nil, fault(422, "EMPTY_QUERY")
		}
		var total int64
		if e = q.Count(&total).Error; e != nil {
			return nil, e
		}
		var skills []Skill
		if e = q.Order("t_ai_skills.name,t_ai_skills.id").Offset((page - 1) * size).Limit(size).Find(&skills).Error; e != nil {
			return nil, e
		}
		out := []JSON{}
		for _, sk := range skills {
			var v Version
			if e = s.DB.WithContext(ctx).First(&v, "id = ?", *sk.ActiveVersionID).Error; e != nil {
				return nil, e
			}
			out = append(out, searchDTO(sk, v, 0))
		}
		return JSON{"skills": out, "total": total, "total_relation": "eq", "mode": mode, "generation_id": sp.ActiveGenerationID}, nil
	}
	if s.Index == nil {
		return nil, fault(503, "SEARCH_UNAVAILABLE")
	}
	if sp.ActiveGenerationID == nil {
		return nil, fault(503, "INDEX_NOT_READY")
	}
	var g Generation
	if e = s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ? AND space_id = ? AND state = 'active'", *sp.ActiveGenerationID, tenant, space).First(&g).Error; e != nil {
		return nil, fault(503, "INDEX_NOT_READY")
	}
	cfg, e := configSnapshot(g)
	if e != nil {
		return nil, e
	}
	var vector []float32
	if mode != "keyword" {
		if cfg.EmbeddingModelID == nil || s.Models == nil {
			return nil, fault(503, "MODEL_UNAVAILABLE")
		}
		vs, e := s.Models.Embed(ctx, tenant, *cfg.EmbeddingModelID, []string{query}, true)
		if e != nil {
			return nil, e
		}
		v, d, e := vectors32(vs, 1)
		if e != nil {
			return nil, e
		}
		if d != g.Dimension {
			return nil, fault(503, "INVALID_DIMENSION")
		}
		vector = v[0]
	}
	hits, e := s.Index.Search(ctx, g.IndexName, query, mode, vector, cfg)
	if e != nil {
		return nil, fault(503, "SEARCH_UNAVAILABLE")
	}
	valid := []Hit{}
	out := map[string]JSON{}
	for _, h := range hits.Hits {
		var sk Skill
		r := q.Session(&gorm.Session{}).Where("t_ai_skills.id = ? AND t_ai_skills.active_version_id = ?", h.SkillID, h.VersionID).First(&sk)
		if errors.Is(r.Error, gorm.ErrRecordNotFound) {
			continue
		}
		if r.Error != nil {
			return nil, r.Error
		}
		var v Version
		if e = s.DB.WithContext(ctx).First(&v, "id = ?", h.VersionID).Error; e != nil {
			return nil, e
		}
		valid = append(valid, h)
		out[h.VersionID] = searchDTO(sk, v, h.Score)
	}
	if cfg.RerankModelID != nil && len(valid) > 0 {
		if s.Models == nil {
			return nil, fault(503, "MODEL_UNAVAILABLE")
		}
		texts := []string{}
		for _, h := range valid {
			texts = append(texts, h.Text)
		}
		scores, e := s.Models.Rerank(ctx, tenant, *cfg.RerankModelID, query, texts)
		if e != nil || len(scores) != len(valid) {
			return nil, fault(503, "RERANK_FAILED")
		}
		for i, n := range scores {
			if math.IsNaN(n) || math.IsInf(n, 0) {
				return nil, fault(503, "RERANK_FAILED")
			}
			valid[i].Score = n
			out[valid[i].VersionID]["score"] = n
		}
		sort.SliceStable(valid, func(i, j int) bool { return valid[i].Score > valid[j].Score })
	}
	result := []JSON{}
	start := min((page-1)*size, len(valid))
	for _, h := range valid[start:min(start+size, len(valid))] {
		result = append(result, out[h.VersionID])
	}
	relation := "eq"
	if hits.Truncated {
		relation = "gte"
	}
	return JSON{"skills": result, "total": len(valid), "total_relation": relation, "mode": mode, "generation_id": g.ID}, nil
}
func searchDTO(sk Skill, v Version, score float64) JSON {
	return JSON{"skill_id": sk.ID, "version_id": v.ID, "name": sk.Name, "description": sk.Description, "tags": sk.Tags, "version": v.Version, "score": score}
}
