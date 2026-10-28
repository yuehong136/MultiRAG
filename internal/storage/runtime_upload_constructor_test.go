package storage

import (
	"context"
	"io"
	"net/http"
	"strconv"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/s3"

	"multirag/internal/server"
)

// Exercise actual constructors before replacing only the SDK transport. The
// effective bucket/prefix fields must come from constructor configuration rather
// than hand-built private fields, and all HTTP stays inside this memory fixture.
func TestRuntimeUploadAdapterConstructedS3UsesConfiguredNamespace(t *testing.T) {
	for _, construction := range []string{"constructor", "factory"} {
		for _, prefix := range []string{"", "prefix"} {
			t.Run(construction+"/"+prefix, func(t *testing.T) {
				t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
				cfg := server.S3Config{
					AccessKey: "owned-test", SecretKey: "owned-test", Region: "us-east-1",
					EndpointURL: "http://storage.invalid", Bucket: "physical", PrefixPath: prefix,
				}
				var original *S3Storage
				if construction == "constructor" {
					store, err := NewS3Storage(&cfg)
					if err != nil {
						t.Fatal(err)
					}
					original = store
				} else {
					factory := &StorageFactory{config: &server.StorageConfig{}}
					if err := factory.initS3(&cfg); err != nil {
						t.Fatal(err)
					}
					var ok bool
					original, ok = factory.GetStorage().(*S3Storage)
					if !ok {
						t.Fatal("S3 factory returned a different storage type")
					}
				}
				originalBucket, originalPrefix := original.bucket, original.prefixPath
				objects := map[string]bool{}
				calls := map[string]int{}
				const content = "runtime"
				expectedObjectPrefix := "/physical/"
				if prefix != "" {
					expectedObjectPrefix += prefix + "/"
				}
				transport := objectProbeTransport(func(request *http.Request) (*http.Response, error) {
					path := request.URL.Path
					calls[request.Method+" "+path]++
					status, body := http.StatusOK, ""
					if request.Method == http.MethodHead && !strings.HasSuffix(path, "/same-id") {
						// Keep every bucket probe in memory, including a wrongly
						// resolved bucket in the pre-fix regression. Returning an
						// error here would trigger legacy reconnect/retry code.
						if path != "/physical" {
							t.Errorf("configured physical bucket lost: HEAD %s, want /physical", path)
						}
					} else {
						if path != expectedObjectPrefix+"alice-downloads/same-id" && path != expectedObjectPrefix+"bob-downloads/same-id" {
							t.Errorf("configured runtime namespace lost: %s %s, want %s<actor>-downloads/same-id", request.Method, path, expectedObjectPrefix)
						}
						switch request.Method {
						case http.MethodPut:
							objects[path] = true
						case http.MethodGet:
							// Get must always return a controlled successful response;
							// assert scope above instead of entering retry/reconnect.
							body = content
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
				// Keep the endpoint, region and credentials produced by the real
				// constructor. Only transport/retry/addressing options change.
				options := original.client.Options()
				options.HTTPClient = &http.Client{Transport: transport}
				options.RetryMaxAttempts = 1
				options.UsePathStyle = true
				original.client = s3.New(options)
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
						t.Fatalf("configured runtime Get failed: %s %v", data, err)
					}
					if exists, err := probe.ObjectExistsContext(t.Context(), actor, "same-id"); !exists || err != nil {
						t.Fatalf("configured runtime probe failed: %t %v", exists, err)
					}
				}
				if len(objects) != 2 {
					t.Fatal("same runtime ID overwrote another actor")
				}
				if err := remover.RemoveContext(t.Context(), "alice-downloads", "same-id"); err != nil {
					t.Fatal(err)
				}
				if exists, err := probe.ObjectExistsContext(t.Context(), "alice-downloads", "same-id"); exists || err != nil {
					t.Fatalf("configured runtime cleanup was not confirmed: %t %v", exists, err)
				}
				if exists, err := probe.ObjectExistsContext(t.Context(), "bob-downloads", "same-id"); !exists || err != nil {
					t.Fatalf("configured runtime cleanup crossed actors: %t %v", exists, err)
				}
				if err := store.Remove("bob-downloads", "same-id"); err != nil {
					t.Fatal(err)
				}
				if len(objects) != 0 {
					t.Fatal("configured runtime Remove used a different mapping")
				}
				if original.bucket != originalBucket || original.prefixPath != originalPrefix || original.config != &cfg {
					t.Fatal("runtime adapter changed the ordinary S3 storage instance")
				}
				for _, actor := range []string{"alice-downloads", "bob-downloads"} {
					path := expectedObjectPrefix + actor + "/same-id"
					for _, method := range []string{http.MethodPut, http.MethodGet, http.MethodHead, http.MethodDelete} {
						if calls[method+" "+path] == 0 {
							t.Errorf("missing configured lifecycle operation: %s %s", method, path)
						}
					}
				}
			})
		}
	}
}
