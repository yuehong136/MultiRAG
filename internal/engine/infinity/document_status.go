package infinity

import (
	"context"
	"errors"
	"fmt"
	infinity "github.com/infiniflow/infinity-go-sdk"
	"multirag/internal/engine/types"
	"sort"
	"strconv"
	"strings"
)

// OpenDocumentStatusStore isolates the complete transaction from the shared
// Thrift client, which is not safe for concurrent use. Its owner closes it only
// after count/update/recovery RPCs have finished, including HTTP cancellation.
func (e *infinityEngine) OpenDocumentStatusStore() (types.DocumentStatusStore, error) {
	client, err := NewInfinityClient(e.config)
	if err != nil {
		return nil, err
	}
	owned := *e
	owned.client = client
	return &owned, nil
}

// documentAvailabilityParents reads every row in the same dataset/doc scope.
// Old mothers have an empty mom_id; their children still reference their ID.
func documentAvailabilityParents(table *infinity.Table, filter string) (string, error) {
	result, err := table.Output([]string{"count(*)"}).Filter(filter).ToResult()
	if err != nil {
		return "", err
	}
	counts, ok := result.(*infinity.QueryResult)
	if !ok || len(counts.Data) != 1 {
		return "", fmt.Errorf("unknown availability relationship count")
	}
	var count int64
	for _, values := range counts.Data {
		if len(values) != 1 {
			return "", fmt.Errorf("unknown availability relationship count")
		}
		count, err = strconv.ParseInt(fmt.Sprint(values[0]), 10, 64)
		if err != nil || count < 0 {
			return "", fmt.Errorf("invalid availability relationship count")
		}
	}
	result, err = table.Output([]string{"id", "doc_id", "mom_id"}).Filter(filter).ToResult()
	if err != nil {
		return "", err
	}
	rows, ok := result.(*infinity.QueryResult)
	if !ok {
		return "", fmt.Errorf("unknown availability relationships")
	}
	return documentParentFilter(rows.Data, count)
}

func documentParentFilter(data map[string][]interface{}, count int64) (string, error) {
	if count < 0 || len(data) != 3 || int64(len(data["id"])) != count || int64(len(data["doc_id"])) != count || int64(len(data["mom_id"])) != count {
		return "", fmt.Errorf("incomplete availability relationships")
	}
	ids, references := map[string]map[string]bool{}, map[string]map[string]bool{}
	for i, value := range data["id"] {
		id, validID := value.(string)
		docID, validDoc := data["doc_id"][i].(string)
		parent, validParent := data["mom_id"][i].(string)
		if !validID || id == "" || !validDoc || docID == "" || !validParent {
			return "", fmt.Errorf("invalid availability relationship row")
		}
		if ids[docID] == nil {
			ids[docID], references[docID] = map[string]bool{}, map[string]bool{}
		}
		if ids[docID][id] {
			return "", fmt.Errorf("duplicate availability relationship row")
		}
		ids[docID][id] = true
		if parent != "" {
			references[docID][parent] = true
		}
	}
	var clauses []string
	for docID, chunkIDs := range ids {
		var parents []string
		for id := range chunkIDs {
			if references[docID][id] {
				parents = append(parents, "'"+escapeFilterValue(id)+"'")
			}
		}
		if len(parents) > 0 {
			sort.Strings(parents)
			clauses = append(clauses, "(doc_id = '"+escapeFilterValue(docID)+"' AND id IN ("+strings.Join(parents, ",")+"))")
		}
	}
	sort.Strings(clauses)
	return strings.Join(clauses, " OR "), nil
}

// DocumentChunkCount distinguishes an absent table/empty document from indexed rows.
func (e *infinityEngine) DocumentChunkCount(ctx context.Context, prefix, datasetID, docID string) (int64, error) {
	db, err := e.client.conn.GetDatabase(e.client.dbName)
	if err != nil {
		return 0, err
	}
	table, err := db.GetTable(prefix + "_" + datasetID)
	if err != nil {
		var missing *infinity.InfinityException
		if errors.As(err, &missing) && infinity.ErrorCode(missing.ErrorCode) == infinity.ErrorCodeTableNotExist {
			return 0, nil
		}
		return 0, err
	}
	filter := equivalentConditionToStr(map[string]interface{}{"doc_id": docID})
	result, err := table.Output([]string{"count(*)"}).Filter(filter).ToResult()
	if err != nil {
		return 0, err
	}
	query, ok := result.(*infinity.QueryResult)
	if !ok {
		return 0, fmt.Errorf("unexpected index count response")
	}
	for _, column := range query.Data {
		if len(column) == 1 {
			return strconv.ParseInt(fmt.Sprint(column[0]), 10, 64)
		}
	}
	return 0, fmt.Errorf("index count unavailable")
}
