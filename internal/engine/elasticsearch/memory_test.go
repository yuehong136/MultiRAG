package elasticsearch

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestMemoryForgetUsesMessageSchemaAndPreservesFirstTime(t *testing.T) {
	var mu sync.Mutex
	forgot := ""
	seq, updates := 0, 0
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.Method == http.MethodGet && r.URL.Path == "/memory_owner/_doc/memory_0" {
			fmt.Fprintf(w, `{"_id":"memory_0","found":true,"_seq_no":%d,"_primary_term":1,"_source":{"memory_id":"memory","message_id":0,"forget_at":%q}}`, seq, forgot)
			return
		}
		if r.Method != http.MethodPost || r.URL.Path != "/memory_owner/_update/memory_0" || r.URL.Query().Get("refresh") != "true" {
			t.Error("wrong memory write route", r.Method, r.URL)
			w.WriteHeader(500)
			return
		}
		if r.URL.Query().Get("if_seq_no") != strconv.Itoa(seq) || r.URL.Query().Get("if_primary_term") != "1" {
			w.WriteHeader(409)
			fmt.Fprint(w, `{"error":"conflict"}`)
			return
		}
		var body struct {
			Script struct {
				Source string `json:"source"`
				Params struct {
					MemoryID  string `json:"memory_id"`
					MessageID int64  `json:"message_id"`
					At        string `json:"at"`
				} `json:"params"`
			} `json:"script"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if strings.Contains(body.Script.Source, "kb_id") || body.Script.Params.MemoryID != "memory" || body.Script.Params.MessageID != 0 {
			t.Error("chunk scope or zero-ID loss", body)
		}
		if forgot == "" {
			forgot = body.Script.Params.At
			seq++
			updates++
		}
		fmt.Fprint(w, `{"result":"updated"}`)
	})
	now := time.Date(2026, 10, 31, 10, 20, 30, 0, time.FixedZone("test", 8*3600))
	if found, err := e.ForgetMemoryMessage(t.Context(), "owner", "memory", 0, now); err != nil || !found {
		t.Fatal(found, err)
	}
	if found, err := e.ForgetMemoryMessage(t.Context(), "owner", "memory", 0, now.Add(time.Hour)); err != nil || !found {
		t.Fatal(found, err)
	}
	if forgot != "2026-10-31 02:20:30" || updates != 1 {
		t.Fatal("timestamp was changed or milliseconds used", forgot, updates)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := e.ForgetMemoryMessage(ctx, "owner", "memory", 0, now); err == nil {
		t.Fatal("canceled write succeeded")
	}
}

func TestMemoryForgetRejectsMissingStoreWrongIdentityAndUnconfirmedWrites(t *testing.T) {
	for _, mode := range []string{"missing-message", "missing-index", "wrong-memory", "wrong-message", "write-failure", "unconfirmed"} {
		t.Run(mode, func(t *testing.T) {
			writes := 0
			e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodGet {
					if mode == "missing-message" {
						w.WriteHeader(404)
						fmt.Fprint(w, `{"found":false}`)
						return
					}
					if mode == "missing-index" {
						w.WriteHeader(404)
						fmt.Fprint(w, `{"error":{"type":"index_not_found_exception"}}`)
						return
					}
					memory, id := "memory", int64(0)
					if mode == "wrong-memory" {
						memory = "other"
					}
					if mode == "wrong-message" {
						id = 1
					}
					fmt.Fprintf(w, `{"_id":"memory_0","found":true,"_seq_no":0,"_primary_term":1,"_source":{"memory_id":%q,"message_id":%d}}`, memory, id)
					return
				}
				writes++
				if mode == "write-failure" {
					w.WriteHeader(500)
					fmt.Fprint(w, `{"error":"controlled failure"}`)
					return
				}
				fmt.Fprint(w, `{"result":"updated"}`)
			})
			found, err := e.ForgetMemoryMessage(t.Context(), "owner", "memory", 0, time.Now())
			if mode == "missing-message" {
				if found || err != nil || writes != 0 {
					t.Fatal(found, err, writes)
				}
			} else if err == nil {
				t.Fatal("missing/unconfirmed storage write succeeded", mode)
			}
			if strings.HasPrefix(mode, "wrong-") && writes != 0 {
				t.Fatal("identity mismatch wrote to store")
			}
		})
	}
}
