package storage

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	"github.com/minio/minio-go/v7"
)

type objectProbeTransport func(*http.Request) (*http.Response, error)

func (f objectProbeTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestObjectExistsContextPreservesUnavailableAndUsesResolvedScope(t *testing.T) {
	for _, kind := range []string{"minio", "s3", "oss"} {
		for _, status := range []int{http.StatusOK, http.StatusNotFound, http.StatusForbidden} {
			t.Run(kind+"/"+http.StatusText(status), func(t *testing.T) {
				calls := 0
				transport := objectProbeTransport(func(request *http.Request) (*http.Response, error) {
					calls++
					expectedPath := "/physical/prefix/actor-downloads/object"
					if kind == "oss" {
						expectedPath = "/physical/prefix/object"
					}
					if request.Method != http.MethodHead || request.URL.Path != expectedPath {
						t.Errorf("probe left resolved scope: %s %s", request.Method, request.URL.Path)
					}
					return &http.Response{
						StatusCode: status,
						Status:     http.StatusText(status),
						Header: http.Header{
							"Content-Length": {"0"},
							"Last-Modified":  {"Mon, 02 Jan 2006 15:04:05 GMT"},
						},
						Body: io.NopCloser(strings.NewReader("")), Request: request,
					}, nil
				})
				awsClient := s3.New(s3.Options{
					BaseEndpoint: aws.String("http://storage.invalid"), Region: "us-east-1",
					Credentials:  credentials.NewStaticCredentialsProvider("test", "test", ""),
					UsePathStyle: true, RetryMaxAttempts: 1, HTTPClient: &http.Client{Transport: transport},
				})
				var probe func(context.Context, string, string) (bool, error)
				switch kind {
				case "minio":
					client, err := minio.New("storage.invalid", &minio.Options{Region: "us-east-1", Transport: transport})
					if err != nil {
						t.Fatal(err)
					}
					probe = (&MinioStorage{client: client, bucket: "physical", prefixPath: "prefix"}).ObjectExistsContext
				case "s3":
					probe = (&S3Storage{client: awsClient, bucket: "physical", prefixPath: "prefix"}).ObjectExistsContext
				case "oss":
					probe = (&OSSStorage{client: awsClient, bucket: "physical", prefixPath: "prefix"}).ObjectExistsContext
				}
				exists, err := probe(t.Context(), "actor-downloads", "object")
				if exists != (status == http.StatusOK) || (err != nil) != (status == http.StatusForbidden) {
					t.Fatalf("status %d mapped to exists=%t, err=%v", status, exists, err)
				}
				if calls != 1 {
					t.Fatalf("probe made %d requests instead of one HEAD", calls)
				}
				ctx, cancel := context.WithCancel(t.Context())
				cancel()
				if exists, err := probe(ctx, "actor-downloads", "object"); exists || !errors.Is(err, context.Canceled) {
					t.Fatalf("cancelled probe succeeded or hid cancellation: %t %v", exists, err)
				}
				if calls != 1 {
					t.Fatal("cancelled probe reached storage")
				}
			})
		}
	}
}

func TestObjectExistsContextTransportErrorIsNotAbsence(t *testing.T) {
	transportError := errors.New("transport unavailable")
	transport := objectProbeTransport(func(*http.Request) (*http.Response, error) { return nil, transportError })
	client := s3.New(s3.Options{
		BaseEndpoint: aws.String("http://storage.invalid"), Region: "us-east-1", UsePathStyle: true, RetryMaxAttempts: 1,
		Credentials: credentials.NewStaticCredentialsProvider("test", "test", ""), HTTPClient: &http.Client{Transport: transport},
	})
	for name, probe := range map[string]func(context.Context, string, string) (bool, error){
		"s3":  (&S3Storage{client: client}).ObjectExistsContext,
		"oss": (&OSSStorage{client: client}).ObjectExistsContext,
	} {
		t.Run(name, func(t *testing.T) {
			exists, err := probe(t.Context(), "bucket", "object")
			if exists || !errors.Is(err, transportError) {
				t.Fatalf("transport failure reported missing: %t %v", exists, err)
			}
		})
	}
}

func TestObjectExistsContextRequiresInitializedClient(t *testing.T) {
	for name, probe := range map[string]func(context.Context, string, string) (bool, error){
		"minio": (&MinioStorage{}).ObjectExistsContext,
		"s3":    (&S3Storage{}).ObjectExistsContext,
		"oss":   (&OSSStorage{}).ObjectExistsContext,
	} {
		t.Run(name, func(t *testing.T) {
			exists, err := probe(t.Context(), "bucket", "object")
			if exists || err == nil {
				t.Fatalf("uninitialized storage reported missing: %t %v", exists, err)
			}
		})
	}
}

func TestObjectNotFoundUsesExplicitProviderCodes(t *testing.T) {
	for _, code := range []string{"NoSuchKey", "NoSuchBucket", "NotFound", "404", "AccessDenied", "InternalError"} {
		t.Run(code, func(t *testing.T) {
			err := &smithy.GenericAPIError{Code: code, Message: "controlled response"}
			want := code != "AccessDenied" && code != "InternalError"
			if isObjectNotFound(err) != want {
				t.Fatalf("unexpected absence classification for %s", code)
			}
		})
	}
	if isObjectNotFound(errors.New("NotFound")) {
		t.Fatal("error text authorized an absence result")
	}
}
