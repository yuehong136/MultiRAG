package elasticsearch

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"multirag/internal/engine/types"
)

// searchPage closes every response, including malformed and partial failures.
func (e *elasticsearchEngine) searchPage(ctx context.Context, indices []string, body map[string]interface{}, exactNumbers bool) (*SearchResponse, error) {
	path := "/_search?allow_partial_search_results=false"
	if len(indices) > 0 {
		path = "/" + escapedIndices(indices) + path
	}
	var result SearchResponse
	if err := e.searchJSON(ctx, http.MethodPost, path, body, &result, exactNumbers); err != nil {
		// A decoded PIT identity remains a resource even if the rest of the
		// response is malformed. The walker must release that newest identity.
		return &result, err
	}
	if result.Hits == nil || result.Hits.Total == nil || result.Hits.Total.Value < 0 || result.Hits.Hits == nil {
		return &result, fmt.Errorf("Elasticsearch omitted required search hits or total")
	}
	if result.TimedOut || result.Shards.Failed != 0 || (result.Hits.Total.Relation != "" && result.Hits.Total.Relation != "eq") {
		return &result, fmt.Errorf("Elasticsearch returned an incomplete search")
	}
	return &result, nil
}

func (e *elasticsearchEngine) searchJSON(ctx context.Context, method, path string, body interface{}, target interface{}, exactNumbers bool) error {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	request, err := http.NewRequestWithContext(ctx, method, path, reader)
	if err != nil {
		return err
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := e.client.Perform(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 {
		return fmt.Errorf("Elasticsearch request failed with HTTP %d", response.StatusCode)
	}
	limited := &io.LimitedReader{R: response.Body, N: (64 << 20) + 1}
	decoder := json.NewDecoder(limited)
	if exactNumbers {
		decoder.UseNumber()
	}
	if err = decoder.Decode(target); err != nil {
		return fmt.Errorf("invalid Elasticsearch response: %w", err)
	}
	if err = decoder.Decode(new(interface{})); err != io.EOF || limited.N == 0 {
		return fmt.Errorf("Elasticsearch response contains trailing or oversized data")
	}
	return nil
}

// walkPIT keeps search_after on one snapshot and releases the latest PIT even
// when cancellation or the visitor aborts the traversal.
func (e *elasticsearchEngine) walkPIT(ctx context.Context, indices []string, body map[string]interface{}, exactNumbers bool, visit func(*SearchResponse) (bool, error)) (err error) {
	var opened struct {
		ID     string `json:"id"`
		Shards struct {
			Failed int `json:"failed"`
		} `json:"_shards"`
	}
	err = e.searchJSON(ctx, http.MethodPost, "/"+escapedIndices(indices)+"/_pit?keep_alive=1m", nil, &opened, false)
	if strings.TrimSpace(opened.ID) == "" {
		if err != nil {
			return err
		}
		return fmt.Errorf("Elasticsearch did not return a PIT identity")
	}
	pitID := opened.ID
	defer func() {
		cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		var closed struct {
			Succeeded bool `json:"succeeded"`
		}
		closeErr := e.searchJSON(cleanupCtx, http.MethodDelete, "/_pit", map[string]interface{}{"id": pitID}, &closed, false)
		if closeErr == nil && !closed.Succeeded {
			closeErr = fmt.Errorf("Elasticsearch did not acknowledge PIT release")
		}
		err = errors.Join(err, closeErr)
	}()
	if err != nil {
		return err
	}
	if opened.Shards.Failed != 0 {
		return fmt.Errorf("Elasticsearch opened an incomplete PIT")
	}
	query := make(map[string]interface{}, len(body)+2)
	for key, value := range body {
		query[key] = value
	}
	delete(query, "from")
	query["size"] = 1000
	orders := []interface{}{}
	switch existing := query["sort"].(type) {
	case []map[string]interface{}:
		for _, order := range existing {
			orders = append(orders, order)
		}
	case []interface{}:
		orders = append(orders, existing...)
	}
	if len(orders) == 0 && (query["knn"] != nil || query["query"] != nil) {
		orders = append(orders, map[string]interface{}{"_score": "desc"})
	}
	hasShardDoc := false
	for _, order := range orders {
		if fields, ok := order.(map[string]interface{}); ok {
			if _, found := fields["_shard_doc"]; found {
				hasShardDoc = true
			}
		}
	}
	if !hasShardDoc {
		orders = append(orders, map[string]interface{}{"_shard_doc": "asc"})
	}
	query["sort"] = orders
	query["track_total_hits"] = true
	var previous string
	for {
		if err = ctx.Err(); err != nil {
			return err
		}
		query["pit"] = map[string]interface{}{"id": pitID, "keep_alive": "1m"}
		page, pageErr := e.searchPage(ctx, nil, query, exactNumbers)
		if page != nil && page.PITID != "" {
			pitID = page.PITID
		}
		if pageErr != nil {
			return pageErr
		}
		done, visitErr := visit(page)
		if visitErr != nil {
			return visitErr
		}
		if done || len(page.Hits.Hits) == 0 {
			return nil
		}
		cursor := page.Hits.Hits[len(page.Hits.Hits)-1].Sort
		if len(cursor) == 0 {
			return fmt.Errorf("Elasticsearch omitted the search_after cursor")
		}
		raw, cursorErr := json.Marshal(cursor)
		if cursorErr != nil {
			return cursorErr
		}
		if string(raw) == previous {
			return fmt.Errorf("Elasticsearch search_after cursor made no progress")
		}
		previous = string(raw)
		query["search_after"] = cursor
	}
}

func (e *elasticsearchEngine) searchDeep(ctx context.Context, indices []string, body map[string]interface{}, offset, limit int, exactNumbers bool) (*SearchResponse, error) {
	var result SearchResponse
	var seen int64
	first := true
	err := e.walkPIT(ctx, indices, body, exactNumbers, func(page *SearchResponse) (bool, error) {
		if first {
			result.Hits = &searchHits{Total: page.Hits.Total, Hits: []searchHit{}}
			first = false
		} else if *result.Hits.Total != *page.Hits.Total {
			return false, fmt.Errorf("Elasticsearch snapshot total changed")
		}
		for _, hit := range page.Hits.Hits {
			if seen >= int64(offset) && len(result.Hits.Hits) < limit {
				result.Hits.Hits = append(result.Hits.Hits, hit)
			}
			seen++
		}
		if seen > result.Hits.Total.Value {
			return false, fmt.Errorf("Elasticsearch returned more hits than its total")
		}
		if len(page.Hits.Hits) == 0 && seen < result.Hits.Total.Value {
			return false, fmt.Errorf("Elasticsearch snapshot ended before its total")
		}
		return len(result.Hits.Hits) == limit || seen == result.Hits.Total.Value, nil
	})
	if err != nil {
		return nil, err
	}
	return &result, nil
}

// ScanMetadata streams exact JSON scalar values from a complete scoped PIT.
// Predicate evaluation remains with the service's original-value matcher: ES
// dynamic mappings cannot preserve every multivalue, missing and numeric rule.
func (e *elasticsearchEngine) ScanMetadata(ctx context.Context, req *types.SearchRequest, visit func([]map[string]interface{}) error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if req == nil || visit == nil || len(req.IndexNames) != 1 || !strings.HasPrefix(req.IndexNames[0], "multirag_doc_meta_") || len(req.KbIDs) == 0 {
		return fmt.Errorf("metadata scan requires one metadata index and explicit datasets")
	}
	if err := validateIndex(req.IndexNames[0]); err != nil {
		return err
	}
	for _, kb := range req.KbIDs {
		if strings.TrimSpace(kb) == "" {
			return fmt.Errorf("metadata dataset scope is empty")
		}
	}
	filter, err := conditionQuery(req.Filter)
	if err != nil {
		return err
	}
	body := map[string]interface{}{
		"query":   map[string]interface{}{"bool": map[string]interface{}{"filter": []interface{}{map[string]interface{}{"terms": map[string]interface{}{"kb_id": req.KbIDs}}, filter}}},
		"_source": []string{"id", "kb_id", "meta_fields"},
		"sort":    []interface{}{map[string]interface{}{"_shard_doc": "asc"}},
	}
	var seen, total int64
	first := true
	return e.walkPIT(ctx, req.IndexNames, body, true, func(page *SearchResponse) (bool, error) {
		if first {
			total = page.Hits.Total.Value
			first = false
		} else if total != page.Hits.Total.Value {
			return false, fmt.Errorf("metadata snapshot total changed")
		}
		seen += int64(len(page.Hits.Hits))
		if seen > total || (len(page.Hits.Hits) == 0 && seen < total) {
			return false, fmt.Errorf("metadata snapshot returned an incomplete total")
		}
		if err := visit(convertESResponse(page, "")); err != nil {
			return false, err
		}
		return seen == total, nil
	})
}

// Preserve literal index bytes when building transport paths. In particular,
// an index containing "%2A" must not become a wildcard after URL decoding.
func escapedIndices(indices []string) string {
	escaped := make([]string, len(indices))
	for i, index := range indices {
		escaped[i] = url.PathEscape(index)
	}
	return strings.Join(escaped, ",")
}
