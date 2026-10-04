package cli

import (
	"context"
	"encoding/json"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func skillTestClient(t *testing.T, handler http.HandlerFunc) *HTTPClient {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	parsed, _ := url.Parse(server.URL)
	client := NewHTTPClient()
	client.Host = parsed.Hostname()
	client.Port, _ = strconv.Atoi(parsed.Port())
	client.APIKey = "test-credential"
	return client
}

func TestSkillsStrictResponseAndCredentials(t *testing.T) {
	for _, test := range []struct {
		name   string
		status int
		body   string
		ok     bool
	}{
		{"accepted", 202, `{"code":0,"message":"success","data":{"operation_id":"id"}}`, true},
		{"business failure", 202, `{"code":102,"message":"partial","data":{}}`, false},
		{"wrong status", 200, `{"code":0,"data":{}}`, false},
		{"missing code", 202, `{"data":{}}`, false},
		{"not JSON", 202, `<html>ok</html>`, false},
		{"conflict", 409, `{"code":409,"message":"conflict","data":{"error_code":"VERSION_CONFLICT"}}`, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer test-credential" || r.Header.Get("Idempotency-Key") != "same-key" {
					t.Error("missing credentials/key")
				}
				if r.URL.Path != "/api/v1/skill-assets/spaces/id/reindex" {
					t.Error(r.URL.Path)
				}
				w.WriteHeader(test.status)
				io.WriteString(w, test.body)
			})
			_, err := client.skillsJSON(context.Background(), "POST", "/spaces/id/reindex", "same-key", nil, 202)
			if (err == nil) != test.ok {
				t.Fatalf("unexpected outcome %v", err)
			}
		})
	}
}

func TestSkillWaitFailsForPartialDirectDTO(t *testing.T) {
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"code":0,"data":{"state":"partial","result":{"items":[]}}}`)
	})
	data, err := client.waitSkillOperation(context.Background(), strings.Repeat("a", 32))
	if err == nil || len(data) == 0 {
		t.Fatal("partial operation reported success or lost detail")
	}
}

func TestSkillsArgsPreserveCommandFlags(t *testing.T) {
	args, err := ParseConnectionArgs([]string{"-t", "token", "skills", "install", strings.Repeat("a", 32), "space in path", "my-skill", "1.0.0", "--activate", "--key", "idempotent"})
	if err != nil {
		t.Fatal(err)
	}
	if args.IsSQLMode || args.CommandArgs[3] != "space in path" || !strings.Contains(args.Command, "--key") {
		t.Fatalf("lost argument boundaries: %#v", args)
	}
}

func TestSkillUploadOrderedManifestAndSymlinkRejection(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "nested"), 0700); err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string]string{"SKILL.md": "---\nname: demo\ndescription: Demo\n---\nbody", "nested/中文.txt": "nested bytes"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	body, kind, err := buildSkillUpload(root, "demo", "1.0.0", true)
	if err != nil {
		t.Fatal(err)
	}
	_, params, _ := mime.ParseMediaType(kind)
	reader := multipart.NewReader(body, params["boundary"])
	part, err := reader.NextPart()
	if err != nil || part.FormName() != "manifest" {
		t.Fatal("manifest missing")
	}
	var manifest struct {
		Files    []skillUploadFile `json:"files"`
		Activate bool              `json:"activate"`
	}
	if err = json.NewDecoder(part).Decode(&manifest); err != nil {
		t.Fatal(err)
	}
	if !manifest.Activate || len(manifest.Files) != 2 || manifest.Files[1].Path != "nested/中文.txt" {
		t.Fatalf("bad manifest %#v", manifest)
	}
	for _, entry := range manifest.Files {
		part, err = reader.NextPart()
		if err != nil || part.FormName() != "file" {
			t.Fatal("repeated file part missing")
		}
		data, _ := io.ReadAll(part)
		if len(data) != entry.Size {
			t.Fatal("manifest and bytes diverged")
		}
	}
	if err = os.Symlink(filepath.Join(root, "SKILL.md"), filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	if _, _, err = buildSkillUpload(root, "demo", "1.0.0", false); err == nil {
		t.Fatal("symlink package accepted")
	}
}

func TestSkillDownloadDoesNotOverwriteAndBinaryRejectsError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "result.zip")
	if err := saveSkillDownload(path, []byte("first")); err != nil {
		t.Fatal(err)
	}
	if err := saveSkillDownload(path, []byte("second")); err == nil {
		t.Fatal("existing file overwritten")
	}
	data, _ := os.ReadFile(path)
	if string(data) != "first" {
		t.Fatal("original changed")
	}
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(401)
		io.WriteString(w, `{"code":401,"message":"denied","data":{"error_code":"UNAUTHENTICATED"}}`)
	})
	if _, err := client.skillsRequest(context.Background(), "GET", "/download", "", "", nil, 200, true); err == nil {
		t.Fatal("error envelope saved as download")
	}
}
