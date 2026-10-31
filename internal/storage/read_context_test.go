package storage

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/minio/minio-go/v7"
	"multirag/internal/server"
)

type contextReadBody struct {
	ctx             context.Context
	entered, closed chan struct{}
	once, closeOnce sync.Once
}

func (b *contextReadBody) Read([]byte) (int, error) {
	b.once.Do(func() { close(b.entered) })
	<-b.ctx.Done()
	return 0, b.ctx.Err()
}
func (b *contextReadBody) Close() error { b.closeOnce.Do(func() { close(b.closed) }); return nil }

func TestStorageContextReadCancellationClosesBody(t *testing.T) {
	for _, kind := range []string{"minio", "s3", "oss"} {
		for _, prefix := range []string{"", "prefix"} {
			t.Run(kind+"/"+prefix, func(t *testing.T) {
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				entered, closed := make(chan struct{}), make(chan struct{})
				calls := 0
				transport := objectProbeTransport(func(r *http.Request) (*http.Response, error) {
					calls++
					expected := "/physical/"
					if prefix != "" {
						expected += prefix + "/"
					}
					expected += "alice-downloads/object"
					if r.Method != http.MethodGet || r.URL.Path != expected {
						t.Errorf("wrong owner mapping %s %s expected %s", r.Method, r.URL.Path, expected)
					}
					if _, ok := r.Context().Deadline(); !ok {
						t.Error("unbounded storage read")
					}
					return &http.Response{StatusCode: 200, Status: "200 OK", Request: r, Header: http.Header{"Content-Length": {"100"}, "Last-Modified": {"Mon, 02 Jan 2006 15:04:05 GMT"}}, ContentLength: 100, Body: &contextReadBody{ctx: r.Context(), entered: entered, closed: closed}}, nil
				})
				awsClient := s3.New(s3.Options{BaseEndpoint: aws.String("http://storage.invalid"), Region: "us-east-1", UsePathStyle: true, RetryMaxAttempts: 1, Credentials: credentials.NewStaticCredentialsProvider("test", "test", ""), HTTPClient: &http.Client{Transport: transport}})
				var store Storage
				switch kind {
				case "minio":
					client, err := minio.New("storage.invalid", &minio.Options{Region: "us-east-1", Transport: transport})
					if err != nil {
						t.Fatal(err)
					}
					store = &MinioStorage{client: client, bucket: "physical", prefixPath: prefix}
				case "s3":
					store = &S3Storage{client: awsClient, config: &server.S3Config{Bucket: "physical", PrefixPath: prefix}}
				case "oss":
					store = &OSSStorage{client: awsClient, bucket: "physical", prefixPath: prefix}
				}
				reader := ForRuntimeUploads(store).(ContextReader)
				done := make(chan error, 1)
				go func() { _, err := reader.GetContext(ctx, "alice-downloads", "object"); done <- err }()
				select {
				case <-entered:
				case <-time.After(2 * time.Second):
					t.Fatal("body read never began")
				}
				cancel()
				select {
				case err := <-done:
					if !errors.Is(err, context.Canceled) {
						t.Fatal("cancellation lost", err)
					}
				case <-time.After(2 * time.Second):
					t.Fatal("read did not stop after cancellation")
				}
				select {
				case <-closed:
				case <-time.After(2 * time.Second):
					t.Fatal("read body was not closed")
				}
				previous := calls
				if _, err := reader.GetContext(ctx, "alice-downloads", "object"); !errors.Is(err, context.Canceled) {
					t.Fatal(err)
				}
				if previous != calls {
					t.Fatal("already canceled read reached transport")
				}
			})
		}
	}
}

func TestStorageContextReadAbsenceAndEmpty(t *testing.T) {
	for _, kind := range []string{"minio", "s3", "oss"} {
		for _, status := range []int{200, 404, 403} {
			t.Run(kind+"/"+http.StatusText(status), func(t *testing.T) {
				transport := objectProbeTransport(func(r *http.Request) (*http.Response, error) {
					body := ""
					if status != 200 {
						body = `<Error><Code>` + map[int]string{404: "NoSuchKey", 403: "AccessDenied"}[status] + `</Code></Error>`
					}
					return &http.Response{StatusCode: status, Status: http.StatusText(status), Request: r, Header: http.Header{"Last-Modified": {"Mon, 02 Jan 2006 15:04:05 GMT"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
				})
				awsClient := s3.New(s3.Options{BaseEndpoint: aws.String("http://storage.invalid"), Region: "us-east-1", UsePathStyle: true, RetryMaxAttempts: 1, Credentials: credentials.NewStaticCredentialsProvider("test", "test", ""), HTTPClient: &http.Client{Transport: transport}})
				var reader ContextReader
				switch kind {
				case "minio":
					client, err := minio.New("storage.invalid", &minio.Options{Region: "us-east-1", Transport: transport})
					if err != nil {
						t.Fatal(err)
					}
					reader = &MinioStorage{client: client}
				case "s3":
					reader = &S3Storage{client: awsClient}
				case "oss":
					reader = &OSSStorage{client: awsClient}
				}
				blob, err := reader.GetContext(t.Context(), "bucket", "object")
				if status == 200 {
					if err != nil || len(blob) != 0 {
						t.Fatal(blob, err)
					}
				} else if status == 404 {
					if !errors.Is(err, ErrNotFound) {
						t.Fatal(err)
					}
				} else if err == nil || errors.Is(err, ErrNotFound) {
					t.Fatal("unavailable storage reported absence", err)
				}
			})
		}
	}
}
