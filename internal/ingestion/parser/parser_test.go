package parser

import (
	"archive/zip"
	"bytes"
	"errors"
	"strings"
	"testing"

	"multirag/internal/testsupport"
)

func officeFixture(t *testing.T, parts map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	z := zip.NewWriter(&buf)
	for name, content := range parts {
		w, err := z.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}
func TestBuiltinOfficeExtractionAndConfiguration(t *testing.T) {
	testsupport.NewCase(t)
	cases := []struct {
		file  string
		parts map[string]string
		want  string
	}{
		{"document.DOCX", map[string]string{"word/document.xml": `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>你好</w:t></w:r><w:r><w:t>世界</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>`}, "你好世界\ntable cell"},
		{"slides.pptx", map[string]string{
			"ppt/presentation.xml":            `<p:presentation xmlns:p="p" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="s2"/><p:sldId id="257" r:id="s1"/></p:sldIdLst></p:presentation>`,
			"ppt/_rels/presentation.xml.rels": `<Relationships><Relationship Id="s1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/><Relationship Id="s2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/></Relationships>`,
			"ppt/slides/slide1.xml":           `<sld><p><t>first file</t></p></sld>`, "ppt/slides/slide2.xml": `<sld><p><t>second file</t></p></sld>`}, "second file\n\nfirst file"},
		{"sheet.xlsx", map[string]string{
			"xl/workbook.xml":            `<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Budget" sheetId="1" r:id="s"/></sheets></workbook>`,
			"xl/_rels/workbook.xml.rels": `<Relationships><Relationship Id="s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/custom.xml"/></Relationships>`,
			"xl/sharedStrings.xml":       `<sst><si><r><t>Hello </t></r><r><t>world</t></r></si></sst>`,
			"xl/worksheets/custom.xml":   `<worksheet><sheetData><row><c r="B1" t="s"><v>0</v></c><c r="D1" t="inlineStr"><is><t>value</t></is></c><c r="A2"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>`}, "Budget\nB1=Hello world\tD1=value\tA2=2"},
	}
	for _, c := range cases {
		data := officeFixture(t, c.parts)
		result, err := Parse(c.file, data, map[string]string{"lib_type": Builtin})
		if err != nil || result.Text != c.want || result.Markdown == "" || result.Backend != Builtin {
			t.Fatalf("%s: %#v %v", c.file, result, err)
		}
		for _, config := range []map[string]string{nil, {"lib_type": "unknown"}, {"lib_type": Builtin, "ignored": "true"}} {
			if result, err := Parse(c.file, data, config); err == nil || result != nil {
				t.Fatal("ignored config", config, result, err)
			}
		}
	}
}
func TestParserFailsClosed(t *testing.T) {
	testsupport.NewCase(t)
	for _, file := range []string{"x.doc", "x.pdf", "x.xls", "x.ppt", "x.txt"} {
		if result, err := Parse(file, []byte("not office"), map[string]string{"lib_type": Builtin}); err == nil || result != nil {
			t.Fatal(file, result, err)
		}
	}
	for _, file := range []string{"x.pdf"} {
		if _, err := Parse(file, []byte("bytes"), map[string]string{"lib_type": OfficeOxide}); !errors.Is(err, ErrUnsupported) {
			t.Fatal(file, err)
		}
	}
	for _, data := range [][]byte{nil, []byte("not ZIP"), officeFixture(t, map[string]string{"word/document.xml": "<document>"}), officeFixture(t, map[string]string{"word/document.xml": "<bogus/>"})} {
		if result, err := Parse("bad.docx", data, map[string]string{"lib_type": Builtin}); err == nil || result != nil {
			t.Fatal("malformed data succeeded", result, err)
		}
	}
	data := officeFixture(t, map[string]string{"word/document.xml": strings.Repeat("x", maxExpandedBytes+1)})
	if _, err := Parse("bomb.docx", data, map[string]string{"lib_type": Builtin}); err == nil {
		t.Fatal("expanded ZIP limit ignored")
	}
}
