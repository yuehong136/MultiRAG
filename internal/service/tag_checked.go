package service

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	"multirag/internal/engine/types"
	"multirag/internal/service/nlp"
)

type tagKnowledgebaseReader func(context.Context, string, string) (*Knowledgebase, error)

// LabelQuestionContext authorizes the complete configured tag selection before
// reading any index. Every tag KB is read from its own owner's index.
func (s *MetadataService) LabelQuestionContext(ctx context.Context, userID, question string, kbs []*Knowledgebase) (map[string]float64, error) {
	return s.labelQuestionContext(ctx, userID, question, kbs, s.kbDAO.GetReadableByID)
}

func (s *MetadataService) labelQuestionContext(ctx context.Context, userID, question string, kbs []*Knowledgebase, readKB tagKnowledgebaseReader) (map[string]float64, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ids, topn, err := tagSelection(kbs)
	if err != nil {
		return nil, err
	}
	if len(ids) == 0 {
		return nil, nil
	}
	if strings.TrimSpace(userID) == "" || readKB == nil {
		return nil, fmt.Errorf("tag selection requires an authenticated user")
	}
	selected := make([]*Knowledgebase, 0, len(ids))
	for _, id := range ids {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		kb, err := readKB(ctx, id, userID)
		if err != nil {
			return nil, fmt.Errorf("authorize tag dataset: %w", err)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if kb == nil || kb.ID != id || strings.TrimSpace(kb.TenantID) == "" {
			return nil, fmt.Errorf("tag dataset binding is invalid")
		}
		selected = append(selected, kb)
	}
	if topn == 0 {
		return map[string]float64{}, nil
	}
	if s.docEngine == nil {
		return nil, fmt.Errorf("tag index is unavailable")
	}
	builder := nlp.GetQueryBuilder()
	if builder == nil {
		return nil, fmt.Errorf("tag query builder is unavailable")
	}
	match, _ := builder.Question(question, "qa", 0)
	if match == nil || strings.TrimSpace(match.MatchingText) == "" {
		return map[string]float64{}, nil
	}
	all, relevant := make(map[string]int64), make(map[string]int64)
	for _, kb := range selected {
		counts, err := s.readTagCounts(ctx, kb, nil)
		if err != nil {
			return nil, err
		}
		if err := mergeTagCounts(all, counts); err != nil {
			return nil, err
		}
		counts, err = s.readTagCounts(ctx, kb, match)
		if err != nil {
			return nil, err
		}
		if err := mergeTagCounts(relevant, counts); err != nil {
			return nil, err
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return scoreQuestionTags(all, relevant, topn)
}

func tagSelection(kbs []*Knowledgebase) ([]string, int, error) {
	ids, seen, topn := []string{}, make(map[string]bool), 3
	for _, kb := range kbs {
		if kb == nil {
			return nil, 0, fmt.Errorf("tag source dataset is required")
		}
		if raw, exists := kb.ParserConfig["topn_tags"]; exists {
			value, err := tagLimit(raw)
			if err != nil {
				return nil, 0, err
			}
			topn = value
		}
		raw, exists := kb.ParserConfig["tag_kb_ids"]
		if !exists || raw == nil {
			continue
		}
		var candidates []string
		switch value := raw.(type) {
		case []string:
			candidates = value
		case []interface{}:
			for _, item := range value {
				id, ok := item.(string)
				if !ok {
					return nil, 0, fmt.Errorf("tag dataset IDs must be strings")
				}
				candidates = append(candidates, id)
			}
		default:
			return nil, 0, fmt.Errorf("tag dataset selection must be an array")
		}
		for _, id := range candidates {
			if strings.TrimSpace(id) == "" || strings.TrimSpace(id) != id {
				return nil, 0, fmt.Errorf("tag dataset ID is invalid")
			}
			if !seen[id] {
				seen[id] = true
				ids = append(ids, id)
			}
		}
	}
	return ids, topn, nil
}

func tagLimit(raw interface{}) (int, error) {
	var value int64
	switch number := raw.(type) {
	case int:
		value = int64(number)
	case int64:
		value = number
	case float64:
		if math.IsNaN(number) || math.IsInf(number, 0) || number < 0 || math.Trunc(number) != number || number >= float64(uint64(1)<<63) {
			return 0, fmt.Errorf("topn_tags must be a finite non-negative integer")
		}
		value = int64(number)
	case json.Number:
		parsed, err := strconv.ParseInt(string(number), 10, 64)
		if err != nil {
			return 0, fmt.Errorf("topn_tags must be a finite non-negative integer")
		}
		value = parsed
	default:
		return 0, fmt.Errorf("topn_tags must be a finite non-negative integer")
	}
	if value < 0 || uint64(value) > uint64(^uint(0)>>1) {
		return 0, fmt.Errorf("topn_tags is out of range")
	}
	return int(value), nil
}

func (s *MetadataService) readTagCounts(ctx context.Context, kb *Knowledgebase, match *types.MatchTextExpr) (map[string]int64, error) {
	counts, seen := make(map[string]int64), make(map[string]bool)
	index := "multirag_" + kb.TenantID
	request := &types.SearchRequest{IndexNames: []string{index}, KbIDs: []string{kb.ID}, Limit: 1000, SelectFields: []string{"id", "kb_id", "tag_kwd"}, OrderBy: new(types.OrderByExpr).Asc("id")}
	if match != nil {
		request.MatchExprs = []interface{}{match}
	}
	expected, offset := int64(-1), 0
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		request.Offset = offset
		found, err := s.docEngine.Search(ctx, request)
		if err != nil {
			return nil, fmt.Errorf("tag search failed: %w", err)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if found == nil || found.Total < 0 || uint64(found.Total) > uint64(^uint(0)>>1) || len(found.Chunks) > request.Limit {
			return nil, fmt.Errorf("tag directory returned invalid pagination")
		}
		if expected < 0 {
			expected = found.Total
		} else if found.Total != expected {
			return nil, fmt.Errorf("tag directory changed during pagination")
		}
		if int64(offset)+int64(len(found.Chunks)) > expected {
			return nil, fmt.Errorf("tag directory exceeded its total")
		}
		for _, row := range found.Chunks {
			id, ok := tagScopeString(row["id"])
			rowKB, kbOK := tagScopeString(row["kb_id"])
			if !ok || id == "" || !kbOK || rowKB != kb.ID || seen[id] {
				return nil, fmt.Errorf("tag directory scope or identity mismatch")
			}
			if rawIndex, exists := row["_index"]; exists {
				rowIndex, ok := rawIndex.(string)
				if !ok || rowIndex != index {
					return nil, fmt.Errorf("tag directory owner index mismatch")
				}
			}
			seen[id] = true
			tags, err := checkedTagValues(row["tag_kwd"])
			if err != nil {
				return nil, err
			}
			for _, tag := range tags {
				if counts[tag] == math.MaxInt64 {
					return nil, fmt.Errorf("tag count overflow")
				}
				counts[tag]++
			}
		}
		offset += len(found.Chunks)
		if int64(offset) == expected {
			return counts, nil
		}
		if len(found.Chunks) == 0 {
			return nil, fmt.Errorf("tag directory pagination made no progress")
		}
	}
}

func tagScopeString(raw interface{}) (string, bool) {
	switch value := raw.(type) {
	case string:
		return value, true
	case []string:
		if len(value) == 1 {
			return value[0], true
		}
	case []interface{}:
		if len(value) == 1 {
			text, ok := value[0].(string)
			return text, ok
		}
	}
	return "", false
}

func checkedTagValues(raw interface{}) ([]string, error) {
	values := []string{}
	switch value := raw.(type) {
	case nil:
		return values, nil
	case string:
		separator := ","
		if strings.Contains(value, "###") {
			separator = "###"
		}
		values = strings.Split(value, separator)
	case []string:
		values = value
	case []interface{}:
		for _, item := range value {
			text, ok := item.(string)
			if !ok {
				return nil, fmt.Errorf("tag directory returned an invalid tag")
			}
			values = append(values, text)
		}
	default:
		return nil, fmt.Errorf("tag directory returned an invalid tag")
	}
	result := make([]string, 0, len(values))
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			result = append(result, value)
		}
	}
	return result, nil
}

func mergeTagCounts(target, source map[string]int64) error {
	for key, value := range source {
		if value < 0 || target[key] > math.MaxInt64-value {
			return fmt.Errorf("tag count overflow")
		}
		target[key] += value
	}
	return nil
}

func scoreQuestionTags(all, relevant map[string]int64, topn int) (map[string]float64, error) {
	if topn < 0 {
		return nil, fmt.Errorf("topn_tags must be non-negative")
	}
	allTotal, relevantTotal := int64(0), int64(0)
	for _, entry := range []struct {
		counts map[string]int64
		total  *int64
	}{{all, &allTotal}, {relevant, &relevantTotal}} {
		for _, count := range entry.counts {
			if count < 0 || *entry.total > math.MaxInt64-count {
				return nil, fmt.Errorf("tag count total overflow")
			}
			*entry.total += count
		}
	}
	result := make(map[string]float64)
	if allTotal == 0 || relevantTotal == 0 || topn == 0 {
		return result, nil
	}
	type scoredTag struct {
		tag   string
		score float64
	}
	scores := make([]scoredTag, 0, len(relevant))
	for tag, count := range relevant {
		baseline := .0001
		if allCount := all[tag]; allCount > 0 {
			baseline = (float64(allCount) + 1) / (float64(allTotal) + 1000)
		}
		score := .1 * (float64(count) + 1) / (float64(relevantTotal) + 1000) / math.Max(1e-6, baseline)
		if math.IsNaN(score) || math.IsInf(score, 0) {
			return nil, fmt.Errorf("tag score must be finite")
		}
		scores = append(scores, scoredTag{tag, score})
	}
	sort.Slice(scores, func(i, j int) bool {
		if scores[i].score == scores[j].score {
			return scores[i].tag < scores[j].tag
		}
		return scores[i].score > scores[j].score
	})
	for _, entry := range scores[:min(topn, len(scores))] {
		key, score := strings.ReplaceAll(entry.tag, ".", "_"), math.Max(1, entry.score)
		if previous, exists := result[key]; !exists || score > previous {
			result[key] = score
		}
	}
	return result, nil
}
