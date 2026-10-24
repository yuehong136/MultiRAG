package elasticsearch

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/elastic/go-elasticsearch/v8/esapi"
	"multirag/internal/engine/types"
	"multirag/internal/utility"
)

func validateIndex(index string) error {
	if strings.TrimSpace(index) == "" || strings.ContainsAny(index, "*,/\\?# ") {
		return fmt.Errorf("an exact index name is required")
	}
	return nil
}
func metadataIndex(tenant string) (string, error) {
	if strings.TrimSpace(tenant) == "" {
		return "", fmt.Errorf("tenant is required")
	}
	name := "multirag_doc_meta_" + tenant
	return name, validateIndex(name)
}
func scopeQuery(condition map[string]interface{}, dataset string) (map[string]interface{}, error) {
	query, err := conditionQuery(condition)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(dataset) == "" {
		return nil, fmt.Errorf("dataset scope is required")
	}
	return map[string]interface{}{"bool": map[string]interface{}{"filter": []interface{}{map[string]interface{}{"term": map[string]interface{}{"kb_id": dataset}}, query}}}, nil
}
func conditionQuery(condition map[string]interface{}) (map[string]interface{}, error) {
	clauses := make([]interface{}, 0, len(condition))
	keys := make([]string, 0, len(condition))
	for k := range condition {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, key := range keys {
		value := condition[key]
		if key == "exists" || key == "must_not" {
			if key == "must_not" {
				m, ok := value.(map[string]interface{})
				if !ok || len(m) != 1 || m["exists"] == nil {
					return nil, fmt.Errorf("unsupported must_not condition")
				}
				value = m["exists"]
			}
			field, ok := value.(string)
			if !ok || !types.ValidFieldName(field) {
				return nil, fmt.Errorf("invalid exists field")
			}
			clause := map[string]interface{}{"exists": map[string]interface{}{"field": field}}
			if key == "must_not" {
				clause = map[string]interface{}{"bool": map[string]interface{}{"must_not": []interface{}{clause}}}
			}
			clauses = append(clauses, clause)
			continue
		}
		if !types.ValidFieldName(key) {
			return nil, fmt.Errorf("invalid condition field %q", key)
		}
		values, list, err := types.ConditionValues(value)
		if err != nil {
			return nil, fmt.Errorf("condition %s: %w", key, err)
		}
		if list {
			clauses = append(clauses, map[string]interface{}{"terms": map[string]interface{}{key: values}})
		} else {
			clauses = append(clauses, map[string]interface{}{"term": map[string]interface{}{key: values[0]}})
		}
	}
	if len(clauses) == 0 {
		return map[string]interface{}{"match_all": map[string]interface{}{}}, nil
	}
	return map[string]interface{}{"bool": map[string]interface{}{"filter": clauses}}, nil
}
func responseJSON(res *esapi.Response, target interface{}) error {
	defer res.Body.Close()
	if res.IsError() {
		body, _ := io.ReadAll(io.LimitReader(res.Body, 8192))
		return fmt.Errorf("elasticsearch %s: %s", res.Status(), body)
	}
	if err := json.NewDecoder(res.Body).Decode(target); err != nil {
		return fmt.Errorf("invalid elasticsearch response: %w", err)
	}
	return nil
}
func (e *elasticsearchEngine) indexExists(ctx context.Context, index string) (bool, error) {
	if err := validateIndex(index); err != nil {
		return false, err
	}
	res, err := (&esapi.IndicesExistsRequest{Index: []string{index}}).Do(ctx, e.client)
	if err != nil {
		return false, err
	}
	defer res.Body.Close()
	if res.StatusCode == 404 {
		return false, nil
	}
	if res.StatusCode != 200 {
		return false, fmt.Errorf("index existence check: %s", res.Status())
	}
	return true, nil
}
func (e *elasticsearchEngine) dropIndex(ctx context.Context, index string) error {
	if err := validateIndex(index); err != nil {
		return err
	}
	res, err := (&esapi.IndicesDeleteRequest{Index: []string{index}}).Do(ctx, e.client)
	if err != nil {
		return err
	}
	var result struct {
		Acknowledged bool `json:"acknowledged"`
	}
	if err = responseJSON(res, &result); err != nil {
		return err
	}
	if !result.Acknowledged {
		return fmt.Errorf("index deletion not acknowledged")
	}
	return nil
}
func (e *elasticsearchEngine) createIndex(ctx context.Context, index, mapping string, vectorSizes ...int) error {
	if err := validateIndex(index); err != nil {
		return err
	}
	raw, err := os.ReadFile(filepath.Join(utility.GetProjectBaseDirectory(), "configs", mapping))
	if err != nil {
		return err
	}
	var check map[string]interface{}
	if err = json.Unmarshal(raw, &check); err != nil || check["mappings"] == nil {
		return fmt.Errorf("invalid index mapping %s", mapping)
	}
	if len(vectorSizes) > 0 && vectorSizes[0] > 0 {
		mappings, ok := check["mappings"].(map[string]interface{})
		if !ok {
			return fmt.Errorf("invalid index mappings object")
		}
		properties, ok := mappings["properties"].(map[string]interface{})
		if !ok {
			properties = map[string]interface{}{}
			mappings["properties"] = properties
		}
		properties[fmt.Sprintf("q_%d_vec", vectorSizes[0])] = map[string]interface{}{"type": "dense_vector", "dims": vectorSizes[0], "index": true, "similarity": "cosine"}
		raw, err = json.Marshal(check)
		if err != nil {
			return err
		}
	}
	res, err := (&esapi.IndicesCreateRequest{Index: index, Body: bytes.NewReader(raw)}).Do(ctx, e.client)
	if err != nil {
		return err
	}
	var result struct {
		Acknowledged bool `json:"acknowledged"`
	}
	if err = responseJSON(res, &result); err != nil {
		return err
	}
	if !result.Acknowledged {
		return fmt.Errorf("index creation not acknowledged")
	}
	return nil
}
func storedID(dataset, id string) string {
	sum := sha256.Sum256([]byte(dataset + "\x00" + id))
	return hex.EncodeToString(sum[:])
}
func (e *elasticsearchEngine) bulkInsert(ctx context.Context, index string, documents []map[string]interface{}, dataset string, identities ...map[string]string) ([]string, error) {
	if err := validateIndex(index); err != nil {
		return nil, err
	}
	var body bytes.Buffer
	ids := make([]string, 0, len(documents))
	seen := map[string]bool{}
	for _, doc := range documents {
		id, ok := doc["id"].(string)
		if !ok || strings.TrimSpace(id) == "" {
			return nil, fmt.Errorf("document id is required")
		}
		kb, ok := doc["kb_id"].(string)
		if !ok || strings.TrimSpace(kb) == "" {
			return nil, fmt.Errorf("document kb_id is required")
		}
		if dataset != "" && kb != dataset {
			return nil, fmt.Errorf("document belongs to another dataset")
		}
		sid := storedID(kb, id)
		if len(identities) > 0 {
			if existing, ok := identities[0][sid]; ok {
				sid = existing
			}
		}
		if seen[sid] {
			return nil, fmt.Errorf("duplicate document id")
		}
		seen[sid] = true
		ids = append(ids, id)
		action := map[string]interface{}{"index": map[string]interface{}{"_index": index, "_id": sid}}
		if err := json.NewEncoder(&body).Encode(action); err != nil {
			return nil, err
		}
		if err := json.NewEncoder(&body).Encode(doc); err != nil {
			return nil, err
		}
	}
	if len(documents) == 0 {
		return []string{}, nil
	}
	res, err := (&esapi.BulkRequest{Body: &body, Refresh: "wait_for"}).Do(ctx, e.client)
	if err != nil {
		return nil, err
	}
	var result struct {
		Errors *bool `json:"errors"`
		Items  []map[string]struct {
			Status int             `json:"status"`
			Error  json.RawMessage `json:"error"`
		} `json:"items"`
	}
	if err = responseJSON(res, &result); err != nil {
		return nil, err
	}
	if result.Errors == nil || len(result.Items) != len(documents) {
		return nil, fmt.Errorf("incomplete bulk response")
	}
	failed := []string{}
	for i, item := range result.Items {
		op, ok := item["index"]
		if !ok || op.Status < 200 || op.Status >= 300 || len(op.Error) > 0 && string(op.Error) != "null" {
			failed = append(failed, ids[i])
		}
	}
	if *result.Errors || len(failed) > 0 {
		return failed, fmt.Errorf("bulk insert failed for %d documents", len(failed))
	}
	return failed, nil
}
func (e *elasticsearchEngine) byQuery(ctx context.Context, index string, query, script map[string]interface{}, update bool) (int64, error) {
	if err := validateIndex(index); err != nil {
		return 0, err
	}
	body := map[string]interface{}{"query": query}
	if update {
		body["script"] = script
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return 0, err
	}
	refresh := true
	var res *esapi.Response
	if update {
		res, err = (&esapi.UpdateByQueryRequest{Index: []string{index}, Body: bytes.NewReader(raw), Refresh: &refresh}).Do(ctx, e.client)
	} else {
		res, err = (&esapi.DeleteByQueryRequest{Index: []string{index}, Body: bytes.NewReader(raw), Refresh: &refresh}).Do(ctx, e.client)
	}
	if err != nil {
		return 0, err
	}
	var result struct {
		Updated   *int64            `json:"updated"`
		Deleted   *int64            `json:"deleted"`
		TimedOut  bool              `json:"timed_out"`
		Conflicts int               `json:"version_conflicts"`
		Failures  []json.RawMessage `json:"failures"`
	}
	if err = responseJSON(res, &result); err != nil {
		return 0, err
	}
	if result.TimedOut || result.Conflicts != 0 || len(result.Failures) != 0 {
		return 0, fmt.Errorf("partial query write failure")
	}
	count := result.Deleted
	if update {
		count = result.Updated
	}
	if count == nil || *count < 0 {
		return 0, fmt.Errorf("missing query write count")
	}
	return *count, nil
}
