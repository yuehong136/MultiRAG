package parser

import (
	"io"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestOfficeNativeExtraction(t *testing.T) {
	j := testsupport.NewCase(t)
	dir := os.Getenv("MULTIRAG_OFFICE_FIXTURES")
	if dir == "" {
		t.Fatal("native extraction requires registered MULTIRAG_OFFICE_FIXTURES")
	}
	for _, ext := range []string{"doc", "docx", "ppt", "pptx", "xls", "xlsx"} {
		data, err := os.ReadFile(filepath.Join(dir, "sample."+ext))
		if err != nil {
			t.Fatal(err)
		}
		parser, err := GetParser(utility.FileType(ext), map[string]string{"lib_type": OfficeOxide})
		if err != nil {
			t.Fatal(err)
		}
		j.Event("reserve-stdout-pipe", map[string]any{"format": ext})
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		output := make(chan []byte, 1)
		go func() { raw, _ := io.ReadAll(reader); reader.Close(); output <- raw }()
		previous := os.Stdout
		os.Stdout = writer
		parseErr := parser.Parse("sample."+ext, data)
		os.Stdout = previous
		writer.Close()
		raw := string(<-output)
		if parseErr != nil {
			t.Errorf("%s: %v", ext, parseErr)
			continue
		}
		parts := strings.SplitN(raw, "Document context:", 2)
		if len(parts) != 2 {
			t.Errorf("%s did not extract native text", ext)
			continue
		}
		textAndMarkdown := strings.SplitN(parts[1], "Document Markdown:", 2)
		if len(textAndMarkdown) != 2 || strings.TrimSpace(textAndMarkdown[0]) == "" || strings.TrimSpace(textAndMarkdown[1]) == "" {
			t.Errorf("%s empty native text/markdown", ext)
		}
		j.Event("native-format-extracted", map[string]any{"format": ext, "output_bytes": len(raw)})
	}
}
