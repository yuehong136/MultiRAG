package storage

import (
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
				const content = "runtime"
				transport := objectProbeTransport(func(request *http.Request) (*http.Response, error) {
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
				store := original
				for _, actor := range []string{"alice-downloads", "bob-downloads"} {
					if err := store.Put(actor, "same-id", []byte(content)); err != nil {
						t.Fatal(err)
					}
					data, err := store.Get(actor, "same-id")
					if err != nil || string(data) != content {
						t.Fatalf("runtime Get failed: %s %v", data, err)
					}
					if !store.ObjExist(actor, "same-id") {
						t.Fatal("mapped object missing")
					}
				}
				if len(objects) != 2 {
					t.Fatal("same upload ID overwrote another actor's object")
				}
				if err := store.Remove("alice-downloads", "same-id"); err != nil {
					t.Fatal(err)
				}
				if store.ObjExist("alice-downloads", "same-id") {
					t.Fatal("deleted object still exists")
				}
				if !store.ObjExist("bob-downloads", "same-id") {
					t.Fatal("mapped object missing")
				}
				if err := store.Remove("bob-downloads", "same-id"); err != nil {
					t.Fatal(err)
				}
				if len(objects) != 0 {
					t.Fatal("runtime Remove used a different key mapping")
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
