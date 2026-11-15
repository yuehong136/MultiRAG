package storage

import (
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	"github.com/minio/minio-go/v7"
	"io"
	"net/http"
	"strings"
	"testing"
)

type objectProbeTransport func(*http.Request) (*http.Response, error)

func (f objectProbeTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func consumerStorage(t *testing.T, kind, prefix string, tr http.RoundTripper) Storage {
	t.Helper()
	client := s3.New(s3.Options{BaseEndpoint: aws.String("http://storage.invalid"), Region: "us-east-1", Credentials: credentials.NewStaticCredentialsProvider("test", "test", ""), UsePathStyle: true, RetryMaxAttempts: 1, HTTPClient: &http.Client{Transport: tr}})
	if kind == "s3" {
		return &S3Storage{client: client, bucket: "physical", prefixPath: prefix}
	}
	if kind == "oss" {
		return &OSSStorage{client: client, bucket: "physical", prefixPath: prefix}
	}
	mc, err := minio.New("storage.invalid", &minio.Options{Region: "us-east-1", Transport: tr})
	if err != nil {
		t.Fatal(err)
	}
	return &MinioStorage{client: mc, bucket: "physical", prefixPath: prefix}
}
func TestObjExistUsesResolvedScope(t *testing.T) {
	for _, kind := range []string{"s3", "oss", "minio"} {
		for _, status := range []int{200, 404, 403} {
			t.Run(kind+http.StatusText(status), func(t *testing.T) {
				calls := 0
				store := consumerStorage(t, kind, "prefix", objectProbeTransport(func(r *http.Request) (*http.Response, error) {
					calls++
					expected := "/physical/prefix/actor-downloads/object"
					if kind == "minio" && r.URL.Path == "/physical/" {
						expected = r.URL.Path
					}
					if r.Method != "HEAD" || r.URL.Path != expected {
						t.Errorf("scope %s %s", r.Method, r.URL.Path)
					}
					if _, ok := r.Context().Deadline(); !ok {
						t.Error("unbounded request")
					}
					return &http.Response{StatusCode: status, Request: r, Header: http.Header{"Content-Length": {"0"}, "Last-Modified": {"Mon, 02 Jan 2006 15:04:05 GMT"}}, Body: io.NopCloser(strings.NewReader(""))}, nil
				}))
				if got := store.ObjExist("actor-downloads", "object"); got != (status == 200) {
					t.Fatalf("existence %t status %d", got, status)
				}
				if calls == 0 {
					t.Fatal("missing HEAD")
				}
			})
		}
	}
}
func TestObjectNotFoundUsesExplicitProviderCodes(t *testing.T) {
	for _, code := range []string{"NoSuchKey", "NoSuchBucket", "NotFound", "404", "AccessDenied", "InternalError"} {
		err := &smithy.GenericAPIError{Code: code, Message: "controlled"}
		want := code != "AccessDenied" && code != "InternalError"
		if isS3NotFound(err) != want || isOSSNotFound(err) != want {
			t.Fatal("classification", code)
		}
	}
}
