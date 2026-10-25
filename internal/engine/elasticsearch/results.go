package elasticsearch

import (
	"html"
	"regexp"
	"sort"
	"strings"
)

func chunkID(chunk map[string]interface{}) string {
	if id, ok := chunk["id"].(string); ok && id != "" {
		return id
	}
	id, _ := chunk["_id"].(string)
	return id
}

// GetDocIDs returns logical chunk IDs in result order, preserving duplicates.
func (e *elasticsearchEngine) GetDocIDs(chunks []map[string]interface{}) []string {
	ids := make([]string, 0, len(chunks))
	for _, chunk := range chunks {
		if id := chunkID(chunk); id != "" {
			ids = append(ids, id)
		}
	}
	return ids
}

func (e *elasticsearchEngine) GetAggregation(chunks []map[string]interface{}, field string) []map[string]interface{} {
	counts := map[string]int{}
	add := func(value string) {
		if value = strings.TrimSpace(value); value != "" {
			counts[value]++
		}
	}
	for _, chunk := range chunks {
		switch value := chunk[field].(type) {
		case string:
			separator := ","
			if field == "tag_kwd" && strings.Contains(value, "###") {
				separator = "###"
			}
			for _, item := range strings.Split(value, separator) {
				add(item)
			}
		case []string:
			for _, item := range value {
				add(item)
			}
		case []interface{}:
			for _, item := range value {
				if text, ok := item.(string); ok {
					add(text)
				}
			}
		}
	}
	keys := make([]string, 0, len(counts))
	for key := range counts {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		if counts[keys[i]] != counts[keys[j]] {
			return counts[keys[i]] > counts[keys[j]]
		}
		return keys[i] < keys[j]
	})
	result := make([]map[string]interface{}, 0, len(keys))
	for _, key := range keys {
		result = append(result, map[string]interface{}{"key": key, "count": counts[key]})
	}
	return result
}

// GetHighlight honors the requested ES highlight field, otherwise highlights
// literal keywords in escaped source text. Only generated <em> markup survives.
func (e *elasticsearchEngine) GetHighlight(chunks []map[string]interface{}, keywords []string, field string) map[string]string {
	normalized := []string{}
	seen := map[string]bool{}
	for _, keyword := range keywords {
		if keyword != "" && !seen[keyword] {
			seen[keyword] = true
			normalized = append(normalized, keyword)
		}
	}
	sort.SliceStable(normalized, func(i, j int) bool { return len(normalized[i]) > len(normalized[j]) })
	parts := make([]string, 0, len(normalized))
	for _, keyword := range normalized {
		parts = append(parts, regexp.QuoteMeta(html.EscapeString(keyword)))
	}
	var pattern *regexp.Regexp
	if len(parts) > 0 {
		pattern = regexp.MustCompile("(?i)(" + strings.Join(parts, "|") + ")")
	}
	result := map[string]string{}
	for _, chunk := range chunks {
		id := chunkID(chunk)
		if id == "" {
			continue
		}
		if highlights, ok := chunk["highlight"].(map[string]interface{}); ok {
			var first string
			switch values := highlights[field].(type) {
			case []interface{}:
				if len(values) > 0 {
					first, _ = values[0].(string)
				}
			case []string:
				if len(values) > 0 {
					first = values[0]
				}
			}
			if first != "" {
				safe := html.EscapeString(first)
				safe = strings.ReplaceAll(safe, "&lt;em&gt;", "<em>")
				safe = strings.ReplaceAll(safe, "&lt;/em&gt;", "</em>")
				result[id] = safe
				continue
			}
		}
		if pattern == nil {
			continue
		}
		text, _ := chunk[field].(string)
		if text == "" && field == "content_with_weight" {
			text, _ = chunk["content"].(string)
		}
		safe := html.EscapeString(strings.ReplaceAll(strings.ReplaceAll(text, "\n", " "), "\r", " "))
		if pattern.MatchString(safe) {
			result[id] = pattern.ReplaceAllStringFunc(safe, func(match string) string { return "<em>" + match + "</em>" })
		}
	}
	return result
}
