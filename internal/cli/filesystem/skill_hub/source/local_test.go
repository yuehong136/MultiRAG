package source

import (
	"github.com/google/uuid"
	"multirag/internal/testsupport"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLocalSourceFetchPreservesUTF8AndSkipsSecrets(t *testing.T) {
	j := testsupport.NewCase(t)
	root := filepath.Join(os.TempDir(), "canonical-skill-"+uuid.NewString())
	j.Event("reserve-skill-directory", map[string]any{"path": root})
	if err := os.Mkdir(root, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(root); err != nil {
			t.Error(err)
		}
		_, err := os.Lstat(root)
		j.Event("cleanup-skill-directory", map[string]any{"path": root, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("skill directory survived")
		}
	})
	markdown := "---\nname: orange\ndescription: 中文描述\nversion: 1.2.3\ntags: [fruit]\n---\n" + strings.Repeat("技能 x\n", 5000)
	for name, data := range map[string]string{"SKILL.md": markdown, "readme.txt": "中文内容", ".env": "credential-canary"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	bundle, err := NewLocalSource().Fetch(root)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Name != "orange" || bundle.Metadata.Description != "中文描述" || string(bundle.Files["SKILL.md"]) != markdown || string(bundle.Files["readme.txt"]) != "中文内容" {
		t.Fatal("metadata/content lost")
	}
	if _, ok := bundle.Files[".env"]; ok {
		t.Fatal("secret file entered skill bundle")
	}
	if err := os.Remove(filepath.Join(root, "SKILL.md")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, ".env"), filepath.Join(root, "SKILL.md")); err != nil {
		t.Fatal(err)
	}
	if _, err := NewLocalSource().Fetch(root); err == nil {
		t.Fatal("symlink manifest escaped bundle boundary")
	}
	if _, err := NewLocalSource().Inspect(root); err == nil {
		t.Fatal("symlink manifest escaped inspection boundary")
	}
}

func TestSourceResolverKeepsCanonicalSources(t *testing.T) {
	resolver := NewSourceResolver(nil)
	for ref, want := range map[string]string{"./local": "local", "github.com/org/repo": "github", "clawhub://owner/skill": "clawhub", "skill://owner/skill": "skills-sh", "https://skills.sh/owner/repo/skill": "skills-sh"} {
		src, id, err := resolver.Resolve(ref)
		if err != nil || src.SourceID() != want || id == "" {
			t.Fatal(ref, id, err)
		}
	}
	if _, _, err := resolver.Resolve(" "); err == nil {
		t.Fatal("empty source accepted")
	}
}
