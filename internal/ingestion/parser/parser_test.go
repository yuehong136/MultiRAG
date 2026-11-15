package parser

import (
	"multirag/internal/utility"
	"testing"
)

func TestParserCanonicalSelectionAndFailures(t *testing.T) {
	for _, typ := range []utility.FileType{utility.FileTypeDOC, utility.FileTypeDOCX, utility.FileTypePPT, utility.FileTypePPTX, utility.FileTypeXLS, utility.FileTypeXLSX} {
		for _, cfg := range []map[string]string{nil, {"lib_type": "unknown"}, {"lib_type": "builtin"}} {
			if _, err := GetParser(typ, cfg); err == nil {
				t.Fatal("unsupported backend accepted", typ, cfg)
			}
		}
		parser, err := GetParser(typ, map[string]string{"lib_type": OfficeOxide})
		if err != nil {
			t.Fatal(err)
		}
		for _, data := range [][]byte{nil, []byte("not an Office file")} {
			if err := parser.Parse("invalid", data); err == nil {
				t.Fatal("corrupt Office bytes accepted", typ)
			}
		}
	}
	pdf, err := GetParser(utility.FileTypePDF, map[string]string{"lib_type": OfficeOxide})
	if err != nil {
		t.Fatal(err)
	}
	if err := pdf.Parse("invalid.pdf", []byte("%PDF")); err == nil {
		t.Fatal("unimplemented PDF returned success")
	}
}
