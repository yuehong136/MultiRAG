package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"multirag/internal/cli/contextengine"
)

func TestSkillCoreWalksEveryDirectoryPageAndUsesExplicitProtocol(t *testing.T) {
	sid := strings.Repeat("a", 32)
	pages := []string{}
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-credential" {
			t.Fatal("missing auth")
		}
		switch r.URL.Path {
		case "/api/v1/skill-core/spaces/" + sid:
			io.WriteString(w, `{"code":0,"data":{"folder_id":"space-root"}}`)
		case "/api/v1/files":
			if r.URL.Query().Get("parent_id") != "space-root" {
				t.Fatalf("used hidden root lookup: %s", r.URL)
			}
			page := r.URL.Query().Get("page")
			pages = append(pages, page)
			rows := []map[string]any{}
			if page == "1" {
				for i := range 100 {
					rows = append(rows, map[string]any{"id": fmt.Sprint(i), "name": fmt.Sprint(i), "type": "folder"})
				}
			} else if page == "2" {
				rows = append(rows, map[string]any{"id": "last", "name": "last", "type": "folder"})
			} else {
				t.Fatal("unexpected page")
			}
			json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": map[string]any{"files": rows, "total": 101}})
		default:
			t.Fatalf("wrong protocol: %s", r.URL)
		}
	})
	provider := contextengine.NewSkillProvider(&skillCoreClient{client: client, ctx: context.Background()})
	result, err := provider.List(context.Background(), sid, &contextengine.ListOptions{Limit: 10, Offset: 100})
	if err != nil || result.Total != 101 || len(result.Nodes) != 1 || result.Nodes[0].Name != "last" || result.HasMore {
		t.Fatalf("truncated listing: %+v %v", result, err)
	}
	if strings.Join(pages, ",") != "1,2" {
		t.Fatal(pages)
	}
}

func TestSkillCoreSearchErrorNeverFallsBackToFiles(t *testing.T) {
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/skill-core/search" {
			t.Fatalf("unexpected fallback: %s", r.URL)
		}
		io.WriteString(w, `{"code":102,"message":"index unavailable","data":{}}`)
	})
	provider := contextengine.NewSkillProvider(&skillCoreClient{client: client, ctx: context.Background()})
	if _, err := provider.Search(context.Background(), strings.Repeat("a", 32), &contextengine.SearchOptions{Query: "x"}); err == nil {
		t.Fatal("business error reported success")
	}
}

func TestSkillCoreIncompletePaginationFails(t *testing.T) {
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"code":0,"data":{"spaces":[],"total":101}}`)
	})
	adapter := &skillCoreClient{client: client, ctx: context.Background()}
	if _, err := adapter.Request("GET", "/skills/spaces", true, "", nil, nil); err == nil {
		t.Fatal("incomplete space catalog accepted")
	}
}

func TestSkillCorePackageRejectsSymlinksAndNestedManifest(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "nested"), 0700); err != nil {
		t.Fatal(err)
	}
	data := []byte("---\nname: demo\ndescription: Demo\n---\nBody")
	if err := os.WriteFile(filepath.Join(dir, "nested", "SKILL.md"), data, 0600); err != nil {
		t.Fatal(err)
	}
	result, _, err := contextengine.ValidateSkillDirectory(dir, "", "")
	if err != nil || result.Valid {
		t.Fatalf("nested manifest accepted: %+v %v", result, err)
	}
	if err = os.WriteFile(filepath.Join(dir, "SKILL.md"), data, 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.Symlink(filepath.Join(dir, "SKILL.md"), filepath.Join(dir, "escape.md")); err != nil {
		t.Fatal(err)
	}
	if _, _, err = contextengine.ValidateSkillDirectory(dir, "", ""); err == nil {
		t.Fatal("symlink accepted")
	}
}

func TestSkillCoreCommandArgumentsRemainDistinct(t *testing.T) {
	for _, command := range []string{"skill-core", "install-skill", "uninstall-skill"} {
		args, err := ParseConnectionArgs([]string{"-t", "credential", command, "space", "path with spaces", "--version", "1.0.0"})
		if err != nil || args.IsSQLMode || len(args.CommandArgs) != 5 || args.CommandArgs[2] != "path with spaces" {
			t.Fatalf("lost command boundaries for %s: %+v %v", command, args, err)
		}
	}
}
