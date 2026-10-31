package elasticsearch

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/elastic/go-elasticsearch/v8/esapi"
	"multirag/internal/engine/types"
)

type memoryMessage struct {
	Found       *bool  `json:"found"`
	ID          string `json:"_id"`
	SeqNo       *int   `json:"_seq_no"`
	PrimaryTerm *int   `json:"_primary_term"`
	Source      struct {
		MemoryID  string      `json:"memory_id"`
		MessageID json.Number `json:"message_id"`
		ForgetAt  *string     `json:"forget_at"`
	} `json:"_source"`
}

func (e *elasticsearchEngine) readMemoryMessage(ctx context.Context, index, id, memoryID string, messageID int64) (*memoryMessage, error) {
	res, err := (esapi.GetRequest{Index: index, DocumentID: id}).Do(ctx, e.client)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	var row memoryMessage
	if err = json.NewDecoder(res.Body).Decode(&row); err != nil {
		return nil, fmt.Errorf("invalid memory message response: %w", err)
	}
	if res.StatusCode == 404 && row.Found != nil && !*row.Found {
		return nil, nil
	}
	if res.IsError() || row.Found == nil || !*row.Found {
		return nil, fmt.Errorf("memory message store read failed: %d", res.StatusCode)
	}
	number, err := row.Source.MessageID.Int64()
	if err != nil || row.ID != id || row.Source.MemoryID != memoryID || number != messageID || row.SeqNo == nil || row.PrimaryTerm == nil {
		return nil, fmt.Errorf("memory message identity is unavailable")
	}
	return &row, nil
}

func memoryMessageForgotten(row *memoryMessage) bool {
	return row.Source.ForgetAt != nil && *row.Source.ForgetAt != "" && *row.Source.ForgetAt != "-"
}

// ForgetMemoryMessage targets the Python physical _id and verifies its memory
// identity. It preserves the first timestamp, checks write acknowledgement and
// independently reads the record back; no kb_id filter is involved.
func (e *elasticsearchEngine) ForgetMemoryMessage(ctx context.Context, tenantID, memoryID string, messageID int64, at time.Time) (bool, error) {
	index, id, err := types.MemoryMessageAddress(tenantID, memoryID, messageID)
	if err != nil {
		return false, err
	}
	if e == nil || e.client == nil {
		return false, types.ErrMemoryWriteUnavailable
	}
	for attempt := 0; attempt < 3; attempt++ {
		if err = ctx.Err(); err != nil {
			return false, err
		}
		row, err := e.readMemoryMessage(ctx, index, id, memoryID, messageID)
		if err != nil {
			return false, err
		}
		if row == nil {
			return false, nil
		}
		if memoryMessageForgotten(row) {
			return true, nil
		}
		body, err := json.Marshal(map[string]any{"script": map[string]any{"lang": "painless", "source": "if (ctx._source.memory_id != params.memory_id || ctx._source.message_id != params.message_id) { throw new IllegalArgumentException('message identity changed'); } if (ctx._source.forget_at == null || ctx._source.forget_at == '' || ctx._source.forget_at == '-') { ctx._source.forget_at = params.at; } else { ctx.op = 'noop'; }", "params": map[string]any{"memory_id": memoryID, "message_id": messageID, "at": at.UTC().Format("2006-01-02 15:04:05")}}})
		if err != nil {
			return false, err
		}
		res, err := (esapi.UpdateRequest{Index: index, DocumentID: id, Body: bytes.NewReader(body), IfSeqNo: row.SeqNo, IfPrimaryTerm: row.PrimaryTerm, Refresh: "true"}).Do(ctx, e.client)
		if err != nil {
			return false, err
		}
		var result struct {
			Result string `json:"result"`
		}
		decodeErr := json.NewDecoder(res.Body).Decode(&result)
		res.Body.Close()
		if res.StatusCode == http.StatusConflict || res.StatusCode == http.StatusNotFound {
			continue
		}
		if res.IsError() || decodeErr != nil || (result.Result != "updated" && result.Result != "noop") {
			return false, fmt.Errorf("memory message write failed: %d", res.StatusCode)
		}
		verified, err := e.readMemoryMessage(ctx, index, id, memoryID, messageID)
		if err != nil {
			return false, err
		}
		if verified == nil || !memoryMessageForgotten(verified) {
			return false, fmt.Errorf("memory message write could not be verified")
		}
		return true, nil
	}
	return false, fmt.Errorf("memory message changed during forgetting; retry")
}
