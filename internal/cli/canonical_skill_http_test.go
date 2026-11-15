package cli

import (
	"encoding/json"
	"fmt"
	"github.com/google/uuid"
	"io"
	"mime"
	"multirag/internal/cli/filesystem"
	"multirag/internal/testsupport"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCanonicalSkillInstallUninstallHTTP(t *testing.T) {
	for _, failure := range []string{"", "index-upload", "index-delete"} {
		t.Run(failure, func(t *testing.T) {
			journal := testsupport.NewCase(t)
			directory := filepath.Join(os.TempDir(), "canonical-cli-skill-"+uuid.NewString())
			journal.Event("reserve-cli-skill-source", map[string]any{"path": directory})
			if err := os.MkdirAll(filepath.Join(directory, "nested"), 0700); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := os.RemoveAll(directory); err != nil {
					t.Error(err)
				}
				_, err := os.Lstat(directory)
				journal.Event("cleanup-cli-skill-source", map[string]any{"path": directory, "absent": os.IsNotExist(err)})
				if !os.IsNotExist(err) {
					t.Error("skill source remained")
				}
			})
			manifest := "---\nname: orange\nversion: 1.2.3\ndescription: Safe example skill\ntags: [fruit]\n---\nHelp safely."
			for path, contents := range map[string]string{"SKILL.md": manifest, "nested/中文.txt": "技能内容", ".env": "secret-canary"} {
				if err := os.WriteFile(filepath.Join(directory, path), []byte(contents), 0600); err != nil {
					t.Fatal(err)
				}
			}
			folders := map[string]map[string]string{"root": {"skills": "skills"}, "skills": {"space": "space"}, "space": {}}
			parentOf := map[string]string{}
			uploads := map[string][]byte{}
			indexed := false
			indexCalls, deleteIndexCalls, deleteFolderCalls := 0, 0, 0
			client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer test-credential" {
					t.Error("skill credential omitted")
				}
				switch r.URL.Path {
				case "/api/v1/files":
					switch r.Method {
					case http.MethodGet:
						parent := r.URL.Query().Get("parent_id")
						if parent == "" {
							parent = "root"
						}
						files := []map[string]any{}
						for name, id := range folders[parent] {
							files = append(files, map[string]any{"id": id, "name": name, "type": "folder"})
						}
						json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": map[string]any{"root_id": "root", "files": files, "total": len(files)}})
					case http.MethodPost:
						if strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/") {
							if err := r.ParseMultipartForm(1 << 20); err != nil {
								t.Error(err)
								return
							}
							defer r.MultipartForm.RemoveAll()
							for _, header := range r.MultipartForm.File["file"] {
								_, params, err := mime.ParseMediaType(header.Header.Get("Content-Disposition"))
								if err != nil {
									t.Error(err)
								}
								source, err := header.Open()
								if err != nil {
									t.Error(err)
									return
								}
								data, err := io.ReadAll(source)
								source.Close()
								if err != nil {
									t.Error(err)
								}
								uploads[params["filename"]] = data
							}
							fmt.Fprint(w, `{"code":0,"data":[]}`)
						} else {
							var body map[string]string
							json.NewDecoder(r.Body).Decode(&body)
							parent := body["parent_id"]
							if parent == "" {
								parent = "root"
							}
							name := body["name"]
							id := "folder-" + name
							folders[parent][name] = id
							folders[id] = map[string]string{}
							parentOf[id] = parent
							fmt.Fprintf(w, `{"code":0,"data":{"id":%q}}`, id)
						}
					case http.MethodDelete:
						deleteFolderCalls++
						var body struct {
							IDs []string `json:"ids"`
						}
						json.NewDecoder(r.Body).Decode(&body)
						for _, id := range body.IDs {
							parent := parentOf[id]
							for name, value := range folders[parent] {
								if value == id {
									delete(folders[parent], name)
								}
							}
							delete(folders, id)
						}
						fmt.Fprint(w, `{"code":0}`)
					default:
						t.Error(r.Method)
					}
				case "/api/v1/skills/spaces":
					fmt.Fprint(w, `{"code":0,"data":{"spaces":[{"id":"space-uuid","name":"space"}]}}`)
				case "/api/v1/skills/config":
					fmt.Fprint(w, `{"code":0,"data":{"embd_id":"embed@instance@provider"}}`)
				case "/api/v1/skills/index":
					if r.Method == http.MethodPost {
						indexCalls++
						var body map[string]any
						json.NewDecoder(r.Body).Decode(&body)
						skills, _ := body["skills"].([]any)
						if body["space_id"] != "space-uuid" || body["embd_id"] != "embed@instance@provider" || len(skills) != 1 {
							t.Error("index payload", body)
						}
						if len(skills) == 1 {
							content := skills[0].(map[string]any)["content"].(string)
							if !strings.Contains(content, "技能内容") || strings.Contains(content, "secret-canary") {
								t.Error("index content escaped bundle", content)
							}
						}
						if failure == "index-upload" {
							fmt.Fprint(w, `{"code":500,"message":"index unavailable"}`)
						} else {
							indexed = true
							fmt.Fprint(w, `{"code":0,"data":{"indexed_count":1}}`)
						}
					}
					if r.Method == http.MethodDelete {
						deleteIndexCalls++
						if r.URL.Query().Get("space_id") != "space-uuid" || r.URL.Query().Get("skill_id") != "orange" {
							t.Error("uninstall scope", r.URL)
						}
						if failure == "index-delete" {
							fmt.Fprint(w, `{"code":500}`)
						} else {
							indexed = false
							fmt.Fprint(w, `{"code":0}`)
						}
					}
				default:
					t.Error("unexpected skill endpoint", r.URL.Path)
					w.WriteHeader(404)
				}
			})
			adapter := &httpClientAdapter{client: client}
			files := filesystem.NewFileProvider(adapter)
			skills := filesystem.NewSkillProvider(adapter)
			install := filesystem.NewInstallSkillCommand(adapter, files, skills)
			err := install.Execute([]string{"space", directory})
			if failure == "index-upload" {
				if err == nil || indexCalls != 1 {
					t.Fatal("index failure acknowledged as install success", err, indexCalls)
				}
				return
			}
			if err != nil || !indexed || indexCalls != 1 || string(uploads["SKILL.md"]) != manifest || string(uploads["nested/中文.txt"]) != "技能内容" || len(uploads) != 2 {
				t.Fatal("canonical installation side effects", err, indexed, indexCalls, uploads)
			}
			uninstall := filesystem.NewUninstallSkillCommand(adapter, skills, files)
			err = uninstall.Execute([]string{"space", "orange"})
			if (err != nil) != (failure == "index-delete") || deleteIndexCalls != 1 || deleteFolderCalls != 1 {
				t.Fatal("uninstall acknowledgement", err, deleteIndexCalls, deleteFolderCalls)
			}
			if failure == "" && indexed {
				t.Fatal("skill index survived uninstall")
			}
			if _, exists := folders["space"]["orange"]; exists {
				t.Fatal("skill directory survived uninstall")
			}
			journal.Event("canonical-skill-http-readback", map[string]any{"uploads": len(uploads), "index_calls": indexCalls, "index_delete_calls": deleteIndexCalls, "folder_delete_calls": deleteFolderCalls, "controlled_error": failure})
		})
	}
}
