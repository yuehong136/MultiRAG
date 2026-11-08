//go:build office_oxide

package parser

import (
	"multirag/internal/testsupport"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestOfficeNativeExtraction(t *testing.T) {
	testsupport.NewCase(t)
	fixtures := os.Getenv("MULTIRAG_OFFICE_FIXTURES")
	if fixtures == "" {
		t.Fatal("native extraction requires registered MULTIRAG_OFFICE_FIXTURES")
	}
	for _, name := range []string{"sample.docx", "sample.xlsx", "sample.pptx", "sample.xls", "sample.ppt", "sample.doc"} {
		data, err := os.ReadFile(filepath.Join(fixtures, name))
		if err != nil {
			t.Fatal(err)
		}
		result, err := Parse(name, data, map[string]string{"lib_type": OfficeOxide})
		if err != nil {
			t.Errorf("%s extraction failed: %v", name, err)
			continue
		}
		if strings.TrimSpace(result.Text) == "" || strings.TrimSpace(result.Markdown) == "" || result.Backend != OfficeOxide {
			t.Errorf("%s empty native output: %#v", name, result)
		}
		t.Logf("%s: format=%s text_bytes=%d markdown_bytes=%d", name, result.Format, len(result.Text), len(result.Markdown))
	}
}
