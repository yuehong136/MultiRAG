package infinity

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	infinity "github.com/infiniflow/infinity-go-sdk"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

func TestMemoryMessageInfinityScratch(t *testing.T) {
	path := os.Getenv("MULTIRAG_AFTERDCF_G_INFINITY_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("isolated Infinity config required")
		}
		t.Skip("requires isolated Infinity Thrift service")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("read private Infinity config")
	}
	var cfg server.InfinityConfig
	if json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("invalid Infinity config")
	}
	host, _, err := net.SplitHostPort(cfg.URI)
	if err != nil || (host != "127.0.0.1" && host != "localhost" && host != "::1") || cfg.DBName != "default_db" {
		t.Fatal("isolated loopback/default_db Infinity required")
	}
	j := testsupport.NewCase(t)
	tenant := "gdcf" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	memoryID := tenant + "memory"
	name := "memory_" + tenant + "_" + memoryID
	j.Event("reserve-infinity-table", map[string]any{"endpoint": cfg.URI, "database": cfg.DBName, "table": name, "config_file": path, "schema": "configs/message_infinity_mapping.json; no kb_id"})
	conn, err := openMemoryConnection(t.Context(), &cfg)
	if err != nil {
		t.Fatal("connect real Infinity SDK", err)
	}
	t.Cleanup(func() { conn.Disconnect() })
	db, err := conn.GetDatabase(cfg.DBName)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.GetTable(name); !missingMemoryTable(err) {
		t.Fatal("scratch table precondition", err)
	}
	t.Cleanup(func() {
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), 10*time.Second)
		defer cancel()
		if _, err := db.DropTable(name, infinity.ConflictTypeIgnore); err != nil {
			t.Error("drop scratch memory table", err)
		}
		verify, err := openMemoryConnection(cleanup, &cfg)
		if err != nil {
			t.Error("independent Infinity cleanup connection", err)
			return
		}
		defer verify.Disconnect()
		checkDB, err := verify.GetDatabase(cfg.DBName)
		if err != nil {
			t.Error("independent Infinity cleanup database", err)
			return
		}
		_, err = checkDB.GetTable(name)
		absent := missingMemoryTable(err)
		j.Event("cleanup-infinity-table", map[string]any{"endpoint": cfg.URI, "database": cfg.DBName, "table": name, "absent": absent})
		if !absent {
			t.Error("independent Infinity cleanup readback", err)
		}
	})
	mapping, err := os.ReadFile("../../../configs/message_infinity_mapping.json")
	if err != nil {
		t.Fatal(err)
	}
	var definitions map[string]struct {
		Type    string      `json:"type"`
		Default interface{} `json:"default"`
		Comment string      `json:"comment"`
	}
	if json.Unmarshal(mapping, &definitions) != nil {
		t.Fatal("parse actual memory schema")
	}
	var keys []string
	for key := range definitions {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var columns infinity.TableSchema
	for _, key := range keys {
		field := definitions[key]
		value := field.Default
		if field.Type == "integer" {
			value = int64(value.(float64))
		}
		columns = append(columns, &infinity.ColumnDefinition{Name: key, DataType: field.Type, Default: value, Comment: field.Comment})
	}
	table, err := db.CreateTable(name, columns, infinity.ConflictTypeError)
	if err != nil {
		t.Fatal("create actual memory schema", err)
	}
	var rows []map[string]interface{}
	for _, id := range []int64{0, 1, 2, 3} {
		memory := memoryID
		if id == 3 {
			memory = "foreign"
		}
		rows = append(rows, map[string]interface{}{"id": fmt.Sprintf("%s_%d", memoryID, id), "memory_id": memory, "message_id": id})
	}
	if _, err := table.Insert(rows); err != nil {
		t.Fatal("seed actual memory rows", err)
	}
	e := &infinityEngine{config: &cfg}
	at := time.Date(2026, 10, 31, 12, 34, 56, 987000000, time.UTC)
	if ok, err := e.ForgetMemoryMessage(t.Context(), tenant, memoryID, 0, at); err != nil || !ok {
		t.Fatal("actual zero-id write", err)
	}
	read := func(id int64) *infinityMemoryMessage {
		t.Helper()
		verify, err := openMemoryConnection(t.Context(), &cfg)
		if err != nil {
			t.Fatal("independent message connection", err)
		}
		defer verify.Disconnect()
		checkDB, err := verify.GetDatabase(cfg.DBName)
		if err != nil {
			t.Fatal(err)
		}
		checkTable, err := checkDB.GetTable(name)
		if err != nil {
			t.Fatal(err)
		}
		row, err := readMemoryMessage(sdkMemoryTable{checkTable}, fmt.Sprintf("%s_%d", memoryID, id), memoryID, id)
		if err != nil {
			t.Fatal("independent actual message readback", err)
		}
		return row
	}
	first := read(0)
	if first == nil || first.forgottenAt != at.Format(memoryTimeFormat) || first.forgottenMillis != float64(float32(at.Truncate(time.Second).UnixMilli())) {
		t.Fatal("actual timestamp milliseconds/float schema", first)
	}
	if ok, err := e.ForgetMemoryMessage(t.Context(), tenant, memoryID, 0, at.Add(time.Hour)); err != nil || !ok {
		t.Fatal("repeat forget", err)
	}
	if again := read(0); *again != *first {
		t.Fatal("first forgetting timestamp changed")
	}
	if neighbor := read(1); neighbor == nil || neighbor.forgottenAt != "" || neighbor.forgottenMillis != 0 {
		t.Fatal("neighbor changed")
	}
	if ok, err := e.ForgetMemoryMessage(t.Context(), tenant, memoryID, 99, at); err != nil || ok {
		t.Fatal("explicit absent row", err)
	}
	if ok, err := e.ForgetMemoryMessage(t.Context(), tenant, memoryID, 3, at); err == nil || ok {
		t.Fatal("foreign identity accepted")
	}
	if ok, err := e.ForgetMemoryMessage(t.Context(), tenant, memoryID+"absent", 0, at); err == nil || ok {
		t.Fatal("missing table treated as success")
	}
	var wg sync.WaitGroup
	failures := make(chan error, 8)
	for i := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ok, err := e.ForgetMemoryMessage(t.Context(), tenant, memoryID, 2, at.Add(time.Duration(i)*time.Hour))
			if err != nil || !ok {
				failures <- fmt.Errorf("concurrent message write: %w", err)
			}
		}()
	}
	wg.Wait()
	close(failures)
	for err := range failures {
		t.Error(err)
	}
	winner := read(2)
	validWinner := false
	for i := range 8 {
		candidate := at.Add(time.Duration(i) * time.Hour).Truncate(time.Second)
		if winner != nil && winner.forgottenAt == candidate.Format(memoryTimeFormat) && winner.forgottenMillis == float64(float32(candidate.UnixMilli())) {
			validWinner = true
		}
	}
	if !validWinner {
		t.Fatal("concurrent timestamp pair not one atomic winner")
	}
	if _, err := db.DropTable(name, infinity.ConflictTypeError); err != nil {
		t.Fatal("drop owned table before missing-store assertion", err)
	}
	if ok, err := e.ForgetMemoryMessage(t.Context(), tenant, memoryID, 0, at); err == nil || ok {
		t.Fatal("removed table acknowledged forgetting")
	}
	j.Event("memory-infinity-verified", map[string]any{"table": name, "message_zero": true, "milliseconds": true, "first_timestamp": true, "concurrent_workers": 8, "missing_table_error": true})
}

func missingMemoryTable(err error) bool {
	var failure *infinity.InfinityException
	return errors.As(err, &failure) && infinity.ErrorCode(failure.ErrorCode) == infinity.ErrorCodeTableNotExist
}
