package parser

import (
	"bytes"
	"encoding/xml"
	"fmt"
	"io"
	"strings"
)

func parseDOCX(a officeArchive) (*Result, error) {
	data, err := a.read("word/document.xml")
	if err != nil {
		return nil, err
	}
	d := xml.NewDecoder(bytes.NewReader(data))
	var lines []string
	var paragraph strings.Builder
	inText := false
	root, body := false, false
	for {
		token, err := d.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("decode DOCX: %w", err)
		}
		switch t := token.(type) {
		case xml.StartElement:
			switch t.Name.Local {
			case "document":
				root = true
			case "body":
				body = true
			case "t":
				inText = true
			case "tab":
				paragraph.WriteString("\t")
			case "br", "cr":
				paragraph.WriteString("\n")
			}
		case xml.CharData:
			if inText {
				paragraph.Write(t)
			}
		case xml.EndElement:
			switch t.Name.Local {
			case "t":
				inText = false
			case "p":
				lines = append(lines, paragraph.String())
				paragraph.Reset()
			}
		}
	}
	if !root || !body {
		return nil, fmt.Errorf("DOCX requires document body")
	}
	text := strings.Join(lines, "\n")
	return &Result{Text: text, Markdown: strings.Join(lines, "\n\n")}, nil
}
