package storage

import (
	"context"
	"errors"
	"github.com/minio/minio-go/v7"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type consumerReadBody struct {
	closed  atomic.Bool
	failure error
}

func (b *consumerReadBody) Read([]byte) (int, error) { return 0, b.failure }
func (b *consumerReadBody) Close() error             { b.closed.Store(true); return nil }
func TestStorageReadFailureClosesBodyAndPreservesCause(t *testing.T) {
	for _, kind := range []string{"s3", "oss", "minio"} {
		t.Run(kind, func(t *testing.T) {
			body := &consumerReadBody{failure: context.Canceled}
			store := consumerStorage(t, kind, "prefix", objectProbeTransport(func(r *http.Request) (*http.Response, error) {
				if r.Method != "GET" || r.URL.Path != "/physical/prefix/alice-downloads/object" {
					t.Fatal("owner mapping", r.URL)
				}
				deadline, ok := r.Context().Deadline()
				if !ok || time.Until(deadline) > 31*time.Second {
					t.Error("unbounded read")
				}
				return &http.Response{StatusCode: 200, Request: r, Header: http.Header{"Content-Length": {"100"}, "Last-Modified": {"Mon, 02 Jan 2006 15:04:05 GMT"}}, ContentLength: 100, Body: body}, nil
			}))
			if _, err := store.Get("alice-downloads", "object"); !errors.Is(err, context.Canceled) {
				t.Fatal("lost cause", err)
			}
			deadline := time.Now().Add(time.Second)
			for !body.closed.Load() && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
			if !body.closed.Load() {
				t.Fatal("body not closed")
			}
		})
	}
}
func TestStorageReadAbsenceAndEmpty(t *testing.T) {
	for _, kind := range []string{"s3", "oss", "minio"} {
		for _, status := range []int{200, 404, 403} {
			t.Run(kind+http.StatusText(status), func(t *testing.T) {
				store := consumerStorage(t, kind, "", objectProbeTransport(func(r *http.Request) (*http.Response, error) {
					body := ""
					if status != 200 {
						body = `<Error><Code>` + map[int]string{404: "NoSuchKey", 403: "AccessDenied"}[status] + `</Code></Error>`
					}
					return &http.Response{StatusCode: status, Request: r, Header: http.Header{"Last-Modified": {"Mon, 02 Jan 2006 15:04:05 GMT"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
				}))
				data, err := store.Get("bucket", "object")
				if status == 200 {
					if err != nil || len(data) != 0 {
						t.Fatal(data, err)
					}
				} else if err == nil {
					t.Fatal("false success", status)
				} else if kind == "minio" {
					code := minio.ToErrorResponse(errors.Unwrap(err)).Code
					if code != map[int]string{404: "NoSuchKey", 403: "AccessDenied"}[status] {
						t.Fatal("provider code lost", err)
					}
				} else if !strings.Contains(err.Error(), map[int]string{404: "NoSuchKey", 403: "AccessDenied"}[status]) {
					t.Fatal("provider cause lost", err)
				}
			})
		}
	}
}
