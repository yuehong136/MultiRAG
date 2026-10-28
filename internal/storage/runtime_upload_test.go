package storage

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

func TestRuntimeUploadAdapterRetainsActorForEntireObjectLifecycle(t *testing.T) {
	for _, kind := range []string{"s3", "oss"} {
		for _, prefix := range []string{"", "prefix"} {
			t.Run(kind+"/"+prefix, func(t *testing.T) {
				objects := map[string]bool{}
				calls := map[string]int{}
				requestCount := 0
				const content = "runtime"
				transport := objectProbeTransport(func(request *http.Request) (*http.Response, error) {
					requestCount++
					path := request.URL.Path
					calls[request.Method+" "+path]++
					status, body := http.StatusOK, ""
					if path == "/physical" && request.Method == http.MethodHead {
						// Existing Put probes the configured bucket first.
					} else {
						expectedPrefix := "/physical/"
						if prefix != "" {
							expectedPrefix += prefix + "/"
						}
						if path != expectedPrefix+"alice-downloads/same-id" && path != expectedPrefix+"bob-downloads/same-id" {
							t.Errorf("runtime operation lost actor namespace: %s %s", request.Method, path)
						}
						switch request.Method {
						case http.MethodPut:
							objects[path] = true
						case http.MethodGet:
							if !objects[path] {
								status = http.StatusNotFound
							} else {
								body = content
							}
						case http.MethodHead:
							if !objects[path] {
								status = http.StatusNotFound
							}
						case http.MethodDelete:
							delete(objects, path)
							status = http.StatusNoContent
						default:
							t.Errorf("unexpected runtime method: %s", request.Method)
						}
					}
					return &http.Response{
						StatusCode: status, Status: http.StatusText(status), Request: request,
						Header:        http.Header{"Content-Length": {strconv.Itoa(len(body))}, "Etag": {`"receipt"`}},
						ContentLength: int64(len(body)), Body: io.NopCloser(strings.NewReader(body)),
					}, nil
				})
				client := s3.New(s3.Options{
					BaseEndpoint: aws.String("http://storage.invalid"), Region: "us-east-1", UsePathStyle: true,
					Credentials:      credentials.NewStaticCredentialsProvider("test", "test", ""),
					RetryMaxAttempts: 1, HTTPClient: &http.Client{Transport: transport},
				})
				var original Storage
				if kind == "s3" {
					original = &S3Storage{client: client, bucket: "physical", prefixPath: prefix}
				} else {
					original = &OSSStorage{client: client, bucket: "physical", prefixPath: prefix}
				}
				store := ForRuntimeUploads(original)
				probe := store.(interface {
					ObjectExistsContext(context.Context, string, string) (bool, error)
				})
				remover := store.(interface {
					RemoveContext(context.Context, string, string, ...string) error
				})
				for _, actor := range []string{"alice-downloads", "bob-downloads"} {
					if err := store.Put(actor, "same-id", []byte(content)); err != nil {
						t.Fatal(err)
					}
					data, err := store.Get(actor, "same-id")
					if err != nil || string(data) != content {
						t.Fatalf("runtime Get failed: %s %v", data, err)
					}
					if exists, err := probe.ObjectExistsContext(t.Context(), actor, "same-id"); !exists || err != nil {
						t.Fatalf("runtime HEAD failed: %t %v", exists, err)
					}
				}
				if len(objects) != 2 {
					t.Fatal("same upload ID overwrote another actor's object")
				}
				if err := remover.RemoveContext(t.Context(), "alice-downloads", "same-id"); err != nil {
					t.Fatal(err)
				}
				if exists, err := probe.ObjectExistsContext(t.Context(), "alice-downloads", "same-id"); exists || err != nil {
					t.Fatalf("alice cleanup was not confirmed: %t %v", exists, err)
				}
				if exists, err := probe.ObjectExistsContext(t.Context(), "bob-downloads", "same-id"); !exists || err != nil {
					t.Fatalf("alice cleanup removed bob's object: %t %v", exists, err)
				}
				if err := store.Remove("bob-downloads", "same-id"); err != nil {
					t.Fatal(err)
				}
				if len(objects) != 0 {
					t.Fatal("runtime Remove used a different key mapping")
				}
				ctx, cancel := context.WithCancel(t.Context())
				cancel()
				before := requestCount
				if err := remover.RemoveContext(ctx, "alice-downloads", "same-id"); !errors.Is(err, context.Canceled) {
					t.Fatalf("cancelled remove succeeded: %v", err)
				}
				if _, err := probe.ObjectExistsContext(ctx, "alice-downloads", "same-id"); !errors.Is(err, context.Canceled) {
					t.Fatalf("cancelled probe succeeded: %v", err)
				}
				if requestCount != before {
					t.Fatal("cancelled operation reached storage")
				}
				for _, actor := range []string{"alice-downloads", "bob-downloads"} {
					path := "/physical/"
					if prefix != "" {
						path += prefix + "/"
					}
					path += actor + "/same-id"
					for _, method := range []string{http.MethodPut, http.MethodGet, http.MethodHead, http.MethodDelete} {
						if calls[method+" "+path] == 0 {
							t.Errorf("missing mapped lifecycle operation: %s %s", method, path)
						}
					}
				}
			})
		}
	}
}

func TestRuntimeUploadAdapterLeavesNamespacePreservingStoresUnchanged(t *testing.T) {
	for name, store := range map[string]Storage{
		"nil": nil, "minio": &MinioStorage{bucket: "physical"}, "s3-bucket-per-actor": &S3Storage{},
		"s3-prefix": &S3Storage{bucket: "physical", prefixPath: "prefix"}, "oss-bucket-per-actor": &OSSStorage{},
	} {
		t.Run(name, func(t *testing.T) {
			if ForRuntimeUploads(store) != store {
				t.Fatal("namespace-preserving store was wrapped")
			}
		})
	}
	s3Store := &S3Storage{bucket: "physical"}
	ossStore := &OSSStorage{bucket: "physical", prefixPath: "prefix"}
	ForRuntimeUploads(s3Store)
	ForRuntimeUploads(ossStore)
	if bucket, key := s3Store.resolveBucketAndPath("ordinary-files", "object"); bucket != "physical" || key != "object" {
		t.Fatal("runtime adapter changed ordinary S3 resolution", bucket, key)
	}
	if bucket, key := ossStore.resolveBucketAndPath("ordinary-files", "object"); bucket != "physical" || key != "prefix/object" {
		t.Fatal("runtime adapter changed ordinary OSS resolution", bucket, key)
	}
}
