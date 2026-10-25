package service

import (
	"context"
	"encoding/json"
	"errors"
	"multirag/internal/engine"
	"multirag/internal/testsupport"
	"strings"
	"testing"
	"time"
)

func TestAfter7fbHealthzStates(t *testing.T) {
	for _, failed := range []string{"", "db", "redis", "doc_engine", "storage", "all"} {
		t.Run(failed, func(t *testing.T) {
			testsupport.NewCase(t)
			calls := 0
			checks := map[string]func(context.Context) error{}
			for _, key := range []string{"db", "redis", "doc_engine", "storage"} {
				key := key
				checks[key] = func(ctx context.Context) error {
					calls++
					if _, ok := ctx.Deadline(); !ok {
						t.Fatal("probe has no deadline")
					}
					if failed == key || failed == "all" {
						return errors.New("private-credential")
					}
					return nil
				}
			}
			result, ok := (&SystemService{healthChecks: checks}).Healthz(t.Context())
			if ok != (failed == "") || calls != 4 {
				t.Fatal(ok, calls, result)
			}
			for key, meta := range result.Meta {
				if meta.Elapsed == "" || (meta.Error != "" && meta.Error != key+" health check failed") {
					t.Fatal(meta)
				}
			}
		})
	}
}

type after7fbInfinityProbe struct{ engine.DocEngine }

func (after7fbInfinityProbe) Type() string               { return "infinity" }
func (after7fbInfinityProbe) Ping(context.Context) error { panic("state-only Ping must not run") }

func TestAfter7fbHealthzFailureReasons(t *testing.T) {
	for _, reason := range []string{"unsupported", "uninitialized", "timeout", "probe_failed"} {
		t.Run(reason, func(t *testing.T) {
			testsupport.NewCase(t)
			ctx := t.Context()
			var cancel context.CancelFunc
			check := func(context.Context) error { return errors.New("private-provider-credential") }
			switch reason {
			case "unsupported":
				check = func(ctx context.Context) error { return documentEngineHealth(ctx, after7fbInfinityProbe{}) }
			case "uninitialized":
				check = func(ctx context.Context) error { return documentEngineHealth(ctx, nil) }
			case "timeout":
				ctx, cancel = context.WithTimeout(ctx, time.Millisecond)
				defer cancel()
				check = func(ctx context.Context) error { <-ctx.Done(); return ctx.Err() }
			}
			result, ok := (&SystemService{healthChecks: map[string]func(context.Context) error{"doc_engine": check}}).Healthz(ctx)
			if ok || result.Meta["doc_engine"].Reason != reason {
				t.Fatal(result)
			}
			raw, err := json.Marshal(result)
			if err != nil || strings.Contains(string(raw), "private-provider") || !strings.Contains(string(raw), `"status":"nok"`) {
				t.Fatal(string(raw), err)
			}
		})
	}
}
