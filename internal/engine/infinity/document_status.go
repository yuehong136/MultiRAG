package infinity

import (
	"context"
	"errors"
	"fmt"
	infinity "github.com/infiniflow/infinity-go-sdk"
	"multirag/internal/engine/types"
	"strconv"
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
