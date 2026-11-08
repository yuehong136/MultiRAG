package parser

import (
	"encoding/xml"
	"fmt"
	"io"
	"strings"
)

func parseDOCX(a *officeArchive) (*Result, error) {
	data, err := a.read("word/document.xml")
	if err != nil {
		return nil, err
	}
	d := newOfficeXMLDecoder(data)
	var plain, markdown strings.Builder
	count := 0
	var paragraph strings.Builder
	inText := false
	root := documentRoot{expected: "document"}
	body := false
	for {
		token, err := d.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("decode DOCX: %w", err)
		}
		if err := root.observe(token); err != nil {
			return nil, err
		}
		switch t := token.(type) {
		case xml.StartElement:
			switch t.Name.Local {
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
				if count > 0 {
					if err := a.emit(&plain, "\n"); err != nil {
						return nil, err
					}
					if err := a.emit(&markdown, "\n\n"); err != nil {
						return nil, err
					}
				}
				if err := a.emit(&plain, paragraph.String()); err != nil {
					return nil, err
				}
				if err := a.emit(&markdown, paragraph.String()); err != nil {
					return nil, err
				}
				count++
				paragraph.Reset()
			}
		}
	}
	if err := root.complete(); err != nil {
		return nil, err
	}
	if !body {
		return nil, fmt.Errorf("DOCX requires document body")
	}
	return &Result{Text: plain.String(), Markdown: markdown.String()}, nil
}
