package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/redis/go-redis/v9"
	"multirag/internal/storage"
)

type documentFileRead struct {
	Bucket string `json:"bucket"`
	Key    string `json:"key"`
}

type documentFileObjects struct {
	storage.Storage
	data  []byte
	err   error
	reads []documentFileRead
	after func()
}

func (s *documentFileObjects) Get(bucket, key string, tenantID ...string) ([]byte, error) {
	s.reads = append(s.reads, documentFileRead{bucket, key})
	if s.after != nil {
		s.after()
	}
	return s.data, s.err
}

func TestDocumentFileStorageAddressRejectsTraversal(t *testing.T) {
	for _, address := range []struct{ bucket, key string }{
		{"", "source"}, {".", "source"}, {"..", "source"}, {"a/b", "source"}, {`a\b`, "source"}, {"a\x00b", "source"},
		{"bucket", ""}, {"bucket", "/source"}, {"bucket", "../source"}, {"bucket", "dir/../source"},
		{"bucket", "dir/./source"}, {"bucket", `dir\source`}, {"bucket", "source\x00"}, {"bucket", "source\n"},
	} {
		if safeStorageAddress(address.bucket, address.key) {
			t.Fatalf("accepted unsafe address %#v", address)
		}
	}
	for _, key := range []string{"source", "nested/source.txt", "a..b", "中文/文档.txt", "with space.txt"} {
		if !safeStorageAddress("dataset-bucket", key) {
			t.Fatalf("rejected registered address %q", key)
		}
	}
}

func TestDocumentFileReadObjectDistinguishesEmptyMissingAndFailure(t *testing.T) {
	for _, test := range []struct {
		name string
		data []byte
		err  error
		want error
	}{
		{"binary", []byte{0, 255, '\n'}, nil, nil},
		{"empty", []byte{}, nil, nil},
		{"missing-nil", nil, nil, ErrDocumentFileUnavailable},
		{"missing-object", nil, storage.ErrNotFound, ErrDocumentFileUnavailable},
		{"missing-bucket", nil, fmt.Errorf("lookup: %w", storage.ErrBucketNotFound), ErrDocumentFileUnavailable},
		{"backend-error", []byte("partial"), errors.New("controlled storage failure"), ErrDocumentFileStorage},
	} {
		t.Run(test.name, func(t *testing.T) {
			objects := &documentFileObjects{data: test.data, err: test.err}
			svc := &DocumentFileService{store: objects}
			got, err := svc.readObject(context.Background(), "bucket", "key")
			if !errors.Is(err, test.want) || len(objects.reads) != 1 || objects.reads[0] != (documentFileRead{"bucket", "key"}) {
				t.Fatalf("read result: err=%v reads=%v", err, objects.reads)
			}
			if test.want == nil && (got == nil || !bytes.Equal(got, test.data)) {
				t.Fatal("valid empty/binary object was lost")
			}
			if test.want != nil && got != nil {
				t.Fatal("failure exposed partial bytes")
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	objects := &documentFileObjects{data: []byte("source")}
	svc := &DocumentFileService{store: objects}
	if got, err := svc.readObject(ctx, "bucket", "key"); !errors.Is(err, context.Canceled) || got != nil || len(objects.reads) != 0 {
		t.Fatal("pre-canceled read reached storage")
	}
	ctx, cancel = context.WithCancel(context.Background())
	defer cancel()
	objects.after = cancel
	if got, err := svc.readObject(ctx, "bucket", "key"); !errors.Is(err, context.Canceled) || got != nil || len(objects.reads) != 1 {
		t.Fatal("in-flight cancellation exposed bytes")
	}
}

func TestDocumentArtifactValidationRejectsBeforeAnyRead(t *testing.T) {
	run, session, file := strings.Repeat("a", 32), strings.Repeat("b", 32), strings.Repeat("c", 32)+".png"
	badSession := "invalid-session"
	for _, test := range []struct {
		name, user, file, run string
		session               *string
	}{
		{"empty-user", "", file, run, nil}, {"bad-run", "actor", file, "bad", nil},
		{"uppercase-run", "actor", file, strings.ToUpper(run), nil},
		{"path", "actor", "../" + file, run, nil}, {"unsupported-extension", "actor", strings.Repeat("c", 32) + ".exe", run, nil},
		{"uppercase-extension", "actor", strings.Repeat("c", 32) + ".PNG", run, nil},
		{"bad-session", "actor", file, run, &badSession}, {"filename-as-session", "actor", file, run, &file},
	} {
		t.Run(test.name, func(t *testing.T) {
			bindings := 0
			objects := &documentFileObjects{data: []byte{}}
			svc := &DocumentFileService{store: objects, bindingReader: func(context.Context, string) (string, error) { bindings++; return "", nil }}
			if got, err := svc.GetDocumentArtifact(context.Background(), test.user, test.file, test.run, test.session); !errors.Is(err, ErrDocumentFileUnavailable) || got != nil || bindings != 0 || len(objects.reads) != 0 {
				t.Fatalf("invalid request reached read boundary: err=%v bindings=%d objects=%d", err, bindings, len(objects.reads))
			}
		})
	}
	if !sameArtifactSession(nil, nil) || sameArtifactSession(nil, &session) || sameArtifactSession(&session, nil) || !sameArtifactSession(&session, &session) || sameArtifactSession(&run, &session) {
		t.Fatal("artifact session equality broadened null scope")
	}
}

func TestDocumentArtifactBindingExactOwnerRunAndNullSession(t *testing.T) {
	run, session, file := strings.Repeat("a", 32), strings.Repeat("b", 32), strings.Repeat("c", 32)+".json"
	for _, raw := range []string{
		`null`, `[]`, `{`, `{}`, `{"owner_id":"actor","run_id":"` + run + `"}`,
		`{"owner_id":"other","run_id":"` + run + `","session_id":null}`,
		`{"owner_id":"actor","run_id":"other","session_id":null}`,
		`{"owner_id":"actor","run_id":"` + run + `","session_id":"` + session + `"}`,
		`{"owner_id":"actor","run_id":"` + run + `","session_id":false}`,
	} {
		t.Run(raw, func(t *testing.T) {
			objects := &documentFileObjects{data: []byte{}}
			bindings := 0
			svc := &DocumentFileService{store: objects, bindingReader: func(_ context.Context, key string) (string, error) {
				bindings++
				if key != "multirag:sandbox_artifact:"+file {
					t.Fatal("wrong artifact ownership key")
				}
				return raw, nil
			}}
			if got, err := svc.GetDocumentArtifact(context.Background(), "actor", file, run, nil); !errors.Is(err, ErrDocumentFileUnavailable) || got != nil || bindings != 1 || len(objects.reads) != 0 {
				t.Fatalf("invalid binding exposed storage: err=%v bindings=%d reads=%d", err, bindings, len(objects.reads))
			}
		})
	}
	for _, test := range []struct {
		name          string
		failure, want error
	}{
		{"missing", redis.Nil, ErrDocumentFileUnavailable},
		{"wrapped-missing", fmt.Errorf("ownership lookup: %w", redis.Nil), ErrDocumentFileUnavailable},
		{"transport", errors.New("controlled Redis transport failure"), ErrDocumentFileStorage},
	} {
		t.Run(test.name, func(t *testing.T) {
			objects := &documentFileObjects{data: []byte{}}
			svc := &DocumentFileService{store: objects, bindingReader: func(context.Context, string) (string, error) { return "", test.failure }}
			if got, err := svc.GetDocumentArtifact(context.Background(), "actor", file, run, nil); !errors.Is(err, test.want) || got != nil || len(objects.reads) != 0 {
				t.Fatal("ownership backend error reached storage", err)
			}
		})
	}
	t.Setenv("SANDBOX_ARTIFACT_BUCKET", "owned-artifacts")
	objects := &documentFileObjects{data: []byte{}}
	raw, err := json.Marshal(artifactBinding{OwnerID: "actor", RunID: run})
	if err != nil {
		t.Fatal(err)
	}
	svc := &DocumentFileService{store: objects, bindingReader: func(context.Context, string) (string, error) { return string(raw), nil }}
	got, err := svc.GetDocumentArtifact(context.Background(), "actor", file, run, nil)
	if err != nil || got == nil || got.Data == nil || len(got.Data) != 0 || got.FileName != file || got.ContentType != "application/json" || len(objects.reads) != 1 || objects.reads[0] != (documentFileRead{"owned-artifacts", file}) {
		t.Fatal("exact null-session binding did not preserve empty artifact", got, err, objects.reads)
	}
	if got, err := svc.GetDocumentArtifact(context.Background(), "actor", file, run, &session); !errors.Is(err, ErrDocumentFileUnavailable) || got != nil || len(objects.reads) != 1 {
		t.Fatal("null bound artifact escaped into a session")
	}
}
