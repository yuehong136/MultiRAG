package storage

import (
	"context"
	"fmt"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"multirag/internal/server"
	"multirag/internal/testsupport"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAfter7fbStorageReadinessIsReadOnly(t *testing.T) {
	for _, kind := range []string{"s3", "oss", "minio"} {
		t.Run(kind, func(t *testing.T) {
			j := testsupport.NewCase(t)
			var calls atomic.Int64
			var fail atomic.Bool
			raw := uuid.New()
			address := fmt.Sprintf("127.0.0.1:%d", 20000+(int(raw[0])*256+int(raw[1]))%30000)
			j.Event("reserve-listener", map[string]any{"address": address, "kind": kind})
			listener, err := net.Listen("tcp", address)
			if err != nil {
				t.Fatal(err)
			}
			live := &httptest.Server{Listener: listener, Config: &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Method != "HEAD" || !strings.HasPrefix(r.URL.Path, "/owned-bucket") {
					t.Error("readiness mutated storage or used wrong scope", r.Method, r.URL.Path)
				}
				if fail.Load() {
					w.WriteHeader(403)
					return
				}
				w.WriteHeader(200)
			})}}
			live.Start()
			defer func() {
				live.Close()
				conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
				if conn != nil {
					conn.Close()
				}
				j.Event("cleanup-listener-readback", map[string]any{"address": address, "absent": err != nil})
				if err == nil {
					t.Error("listener remains")
				}
			}()
			client := s3.New(s3.Options{BaseEndpoint: aws.String(live.URL), Region: "us-east-1", Credentials: credentials.NewStaticCredentialsProvider("owned", "owned", ""), UsePathStyle: true, RetryMaxAttempts: 1})
			var store Storage
			switch kind {
			case "s3":
				store = &S3Storage{client: client, config: &server.S3Config{Bucket: "owned-bucket"}}
			case "oss":
				store = &OSSStorage{client: client, bucket: "owned-bucket"}
			case "minio":
				store, err = NewMinioStorage(&server.MinioConfig{Host: address, User: "owned", Password: "owned-password", Bucket: "owned-bucket", Region: "us-east-1"})
				if err != nil {
					t.Fatal(err)
				}
			}
			if err = readinessFor(t.Context(), store); err != nil {
				t.Fatal(err)
			}
			fail.Store(true)
			if err = readinessFor(t.Context(), store); err == nil {
				t.Fatal("permission failure reported healthy")
			}
			ctx, cancel := context.WithCancel(t.Context())
			cancel()
			if err = readinessFor(ctx, store); err == nil {
				t.Fatal("cancelled probe succeeded")
			}
			j.Event("controlled-storage-readback", map[string]any{"requests": calls.Load(), "methods": "HEAD only", "objects_created": 0})
		})
	}
}
