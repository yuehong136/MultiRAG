package infinity

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net"
	"strconv"
	"time"

	infinity "github.com/infiniflow/infinity-go-sdk"
	"multirag/internal/engine/types"
	"multirag/internal/server"
)

const memoryTimeFormat = "2006-01-02 15:04:05"

type memoryMessageTable interface {
	query([]string, string) (*infinity.QueryResult, error)
	update(string, map[string]interface{}) error
}

type sdkMemoryTable struct{ table *infinity.Table }

func (t sdkMemoryTable) query(columns []string, filter string) (*infinity.QueryResult, error) {
	result, err := t.table.Output(columns).Filter(filter).ToResult()
	if err != nil {
		return nil, err
	}
	rows, ok := result.(*infinity.QueryResult)
	if !ok || rows == nil {
		return nil, fmt.Errorf("invalid memory message response")
	}
	return rows, nil
}

func (t sdkMemoryTable) update(filter string, values map[string]interface{}) error {
	_, err := t.table.Update(filter, values)
	return err
}

func openMemoryConnection(ctx context.Context, cfg *server.InfinityConfig) (*infinity.InfinityConnection, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if cfg == nil || cfg.DBName == "" {
		return nil, types.ErrMemoryWriteUnavailable
	}
	host, textPort, err := net.SplitHostPort(cfg.URI)
	if err != nil || host == "" {
		return nil, fmt.Errorf("invalid Infinity memory endpoint")
	}
	port, err := strconv.Atoi(textPort)
	if err != nil || port < 1 || port > 65535 {
		return nil, fmt.Errorf("invalid Infinity memory port")
	}
	// No shared Thrift client and no 120-second reconnect loop. The current SDK
	// has no cancellable transport API; every accepted RPC is drained in this
	// goroutine, and Disconnect runs before the service releases SQL authority.
	return infinity.Connect(infinity.NetworkAddress{IP: host, Port: port})
}

// ForgetMemoryMessage writes the Python memory table, which has memory_id and
// no kb_id. A private synchronous connection isolates concurrent HTTP requests.
func (e *infinityEngine) ForgetMemoryMessage(ctx context.Context, tenantID, memoryID string, messageID int64, at time.Time) (bool, error) {
	index, id, err := types.MemoryMessageAddress(tenantID, memoryID, messageID)
	if err != nil {
		return false, err
	}
	if e == nil {
		return false, types.ErrMemoryWriteUnavailable
	}
	conn, err := openMemoryConnection(ctx, e.config)
	if err != nil {
		return false, err
	}
	defer conn.Disconnect()
	if err = ctx.Err(); err != nil {
		return false, err
	}
	db, err := conn.GetDatabase(e.config.DBName)
	if err != nil {
		return false, err
	}
	table, err := db.GetTable(index + "_" + memoryID)
	if err != nil {
		return false, err
	} // Missing table is an unavailable store.
	return forgetMemoryMessage(ctx, sdkMemoryTable{table}, id, memoryID, messageID, at)
}

type infinityMemoryMessage struct {
	forgottenAt     string
	forgottenMillis float64
}

func memoryInteger(value interface{}) (int64, bool) {
	switch number := value.(type) {
	case int64:
		return number, true
	case int32:
		return int64(number), true
	case int:
		return int64(number), true
	default:
		return 0, false
	}
}

func readMemoryMessage(table memoryMessageTable, id, memoryID string, messageID int64) (*infinityMemoryMessage, error) {
	filter := "id = '" + escapeFilterValue(id) + "'"
	count, err := table.query([]string{"count(*)"}, filter)
	if err != nil {
		return nil, err
	}
	if count == nil || len(count.Data) != 1 {
		return nil, fmt.Errorf("invalid memory message count")
	}
	var size int64 = -1
	for _, values := range count.Data {
		if len(values) != 1 {
			return nil, fmt.Errorf("invalid memory message count")
		}
		var valid bool
		size, valid = memoryInteger(values[0])
		if !valid || size < 0 || size > 1 {
			return nil, fmt.Errorf("ambiguous memory message identity")
		}
	}
	if size == 0 {
		return nil, nil
	}
	columns := []string{"id", "memory_id", "message_id", "forget_at", "forget_at_flt"}
	result, err := table.query(columns, filter)
	if err != nil {
		return nil, err
	}
	if result == nil || len(result.Data) != len(columns) {
		return nil, fmt.Errorf("invalid memory message shape")
	}
	for _, key := range columns {
		if len(result.Data[key]) != 1 {
			return nil, fmt.Errorf("incomplete memory message shape")
		}
	}
	actualID, validID := result.Data["id"][0].(string)
	actualMemory, validMemory := result.Data["memory_id"][0].(string)
	actualMessage, validMessage := memoryInteger(result.Data["message_id"][0])
	if !validID || !validMemory || !validMessage || actualID != id || actualMemory != memoryID || actualMessage != messageID {
		return nil, fmt.Errorf("memory message identity changed")
	}
	text, validText := result.Data["forget_at"][0].(string)
	var millis float64
	switch value := result.Data["forget_at_flt"][0].(type) {
	case float32:
		millis = float64(value)
	case float64:
		millis = value
	default:
		return nil, fmt.Errorf("invalid memory timestamp type")
	}
	if !validText || math.IsNaN(millis) || math.IsInf(millis, 0) || (text == "" && millis != 0) {
		return nil, fmt.Errorf("inconsistent memory timestamp")
	}
	if text != "" {
		if _, err := time.Parse(memoryTimeFormat, text); err != nil || millis <= 0 {
			return nil, fmt.Errorf("invalid forgotten memory timestamp")
		}
	}
	return &infinityMemoryMessage{text, millis}, nil
}

func forgetMemoryMessage(ctx context.Context, table memoryMessageTable, id, memoryID string, messageID int64, at time.Time) (bool, error) {
	at = at.UTC().Truncate(time.Second)
	if at.UnixMilli() <= 0 {
		return false, fmt.Errorf("invalid memory forgetting time")
	}
	for range 3 {
		if err := ctx.Err(); err != nil {
			return false, err
		}
		before, err := readMemoryMessage(table, id, memoryID, messageID)
		if err != nil {
			return false, err
		}
		if before == nil {
			return false, nil
		}
		if before.forgottenAt != "" {
			if err := ctx.Err(); err != nil {
				return false, err
			}
			return true, nil
		}
		if err := ctx.Err(); err != nil {
			return false, err
		}
		// Keep legitimate message_id=0. The empty timestamp pair makes this an
		// atomic compare-and-set, preserving the first successful writer's time.
		filter := fmt.Sprintf("id = '%s' AND memory_id = '%s' AND message_id = %d AND forget_at = '' AND forget_at_flt = 0", escapeFilterValue(id), escapeFilterValue(memoryID), messageID)
		err = table.update(filter, map[string]interface{}{"forget_at": at.Format(memoryTimeFormat), "forget_at_flt": float64(at.UnixMilli())})
		if err != nil {
			var conflict *infinity.InfinityException
			if errors.As(err, &conflict) && infinity.ErrorCode(conflict.ErrorCode) == infinity.ErrorCodeTXNConflict {
				continue
			}
			return false, err
		}
		// The SDK exposes no affected-row count. Read back even after HTTP
		// cancellation, while SQL authorization is still held; zero-hit is not
		// success. A concurrent winner may legitimately have an earlier time.
		after, err := readMemoryMessage(table, id, memoryID, messageID)
		if err != nil {
			return false, err
		}
		if after == nil || after.forgottenAt == "" {
			return false, fmt.Errorf("memory forgetting could not be confirmed")
		}
		if after.forgottenAt == at.Format(memoryTimeFormat) && after.forgottenMillis != float64(at.UnixMilli()) && after.forgottenMillis != float64(float32(at.UnixMilli())) {
			return false, fmt.Errorf("memory forgetting milliseconds differ")
		}
		if err := ctx.Err(); err != nil {
			return false, err
		}
		return true, nil
	}
	return false, fmt.Errorf("memory message changed during forgetting; retry")
}

var _ types.MemoryMessageStore = (*infinityEngine)(nil)
