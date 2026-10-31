package infinity

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	infinity "github.com/infiniflow/infinity-go-sdk"
)

type fakeMemoryTable struct {
	row                map[string][]interface{}
	count              int64
	readErr, updateErr error
	unchanged          bool
	updates, reads     int
	cancel             context.CancelFunc
}

func memoryTestTable() *fakeMemoryTable {
	return &fakeMemoryTable{count: 1, row: map[string][]interface{}{"id": {"m_0"}, "memory_id": {"m"}, "message_id": {int64(0)}, "forget_at": {""}, "forget_at_flt": {float64(0)}}}
}

func (t *fakeMemoryTable) query(columns []string, filter string) (*infinity.QueryResult, error) {
	t.reads++
	if t.readErr != nil {
		return nil, t.readErr
	}
	if filter != "id = 'm_0'" {
		return nil, errors.New("broadened identity filter")
	}
	if len(columns) == 1 && columns[0] == "count(*)" {
		return &infinity.QueryResult{Data: map[string][]interface{}{"count(*)": {t.count}}}, nil
	}
	return &infinity.QueryResult{Data: t.row}, nil
}

func (t *fakeMemoryTable) update(filter string, values map[string]interface{}) error {
	t.updates++
	if filter != "id = 'm_0' AND memory_id = 'm' AND message_id = 0 AND forget_at = '' AND forget_at_flt = 0" || len(values) != 2 || !strings.Contains(filter, "message_id = 0") {
		return errors.New("unsafe memory CAS")
	}
	if t.cancel != nil {
		t.cancel()
	}
	if t.updateErr != nil {
		return t.updateErr
	}
	if !t.unchanged {
		t.row["forget_at"] = []interface{}{values["forget_at"]}
		t.row["forget_at_flt"] = []interface{}{float64(float32(values["forget_at_flt"].(float64)))}
	}
	return nil
}

func TestMemoryMessageCASAndFailureContracts(t *testing.T) {
	at := time.Date(2026, 10, 31, 12, 34, 56, 987000000, time.UTC)
	table := memoryTestTable()
	if changed, err := forgetMemoryMessage(t.Context(), table, "m_0", "m", 0, at); err != nil || !changed {
		t.Fatal("message zero write", err)
	}
	first := table.row["forget_at"][0]
	if table.row["forget_at_flt"][0] != float64(float32(at.Truncate(time.Second).UnixMilli())) {
		t.Fatal("milliseconds/float32 schema")
	}
	if changed, err := forgetMemoryMessage(t.Context(), table, "m_0", "m", 0, at.Add(time.Hour)); err != nil || !changed || table.updates != 1 || table.row["forget_at"][0] != first {
		t.Fatal("first timestamp overwritten")
	}
	for _, test := range []struct {
		name   string
		mutate func(*fakeMemoryTable)
	}{
		{"wrong memory", func(f *fakeMemoryTable) { f.row["memory_id"][0] = "foreign" }},
		{"wrong physical id", func(f *fakeMemoryTable) { f.row["id"][0] = "foreign_0" }},
		{"wrong message", func(f *fakeMemoryTable) { f.row["message_id"][0] = int64(1) }},
		{"string number", func(f *fakeMemoryTable) { f.row["message_id"][0] = "0" }},
		{"truncated", func(f *fakeMemoryTable) { f.row["memory_id"] = nil }},
		{"missing time", func(f *fakeMemoryTable) { delete(f.row, "forget_at_flt") }},
		{"NaN", func(f *fakeMemoryTable) { f.row["forget_at_flt"][0] = math.NaN() }},
		{"mismatched pair", func(f *fakeMemoryTable) { f.row["forget_at_flt"][0] = float64(1) }},
		{"duplicate", func(f *fakeMemoryTable) { f.count = 2 }},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := memoryTestTable()
			test.mutate(f)
			if changed, err := forgetMemoryMessage(t.Context(), f, "m_0", "m", 0, at); err == nil || changed || f.updates != 0 {
				t.Fatal("unsafe message shape accepted")
			}
		})
	}
	table = memoryTestTable()
	table.count = 0
	if changed, err := forgetMemoryMessage(t.Context(), table, "m_0", "m", 0, at); err != nil || changed || table.updates != 0 {
		t.Fatal("missing row contract")
	}
	for _, test := range []struct {
		name               string
		readErr, updateErr error
		unchanged          bool
	}{
		{"read unavailable", errors.New("read failed"), nil, false},
		{"write unavailable", nil, errors.New("write failed"), false},
		{"zero hit", nil, nil, true},
		{"conflict exhausted", nil, infinity.NewInfinityException(int(infinity.ErrorCodeTXNConflict), "conflict"), false},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := memoryTestTable()
			f.readErr = test.readErr
			f.updateErr = test.updateErr
			f.unchanged = test.unchanged
			if changed, err := forgetMemoryMessage(t.Context(), f, "m_0", "m", 0, at); err == nil || changed {
				t.Fatal("failed write acknowledged")
			}
		})
	}
}

func TestMemoryMessageCancellationDrainsReadback(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	table := memoryTestTable()
	if _, err := forgetMemoryMessage(ctx, table, "m_0", "m", 0, time.Now()); !errors.Is(err, context.Canceled) || table.reads != 0 {
		t.Fatal("pre-cancel issued RPC")
	}
	ctx, cancel = context.WithCancel(t.Context())
	defer cancel()
	table = memoryTestTable()
	table.cancel = cancel
	if changed, err := forgetMemoryMessage(ctx, table, "m_0", "m", 0, time.Now()); !errors.Is(err, context.Canceled) || changed || table.reads != 4 || table.updates != 1 {
		t.Fatal("accepted RPC readback did not drain", table.reads, err)
	}
}
