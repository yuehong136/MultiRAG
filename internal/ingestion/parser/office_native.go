//go:build office_oxide

package parser

import (
	"fmt"
	office "github.com/yfedoseev/office_oxide/go"
)

func parseOfficeOxide(format string, data []byte) (*Result, error) {
	if format == "docx" || format == "pptx" || format == "xlsx" {
		if _, err := openOfficeArchive(data); err != nil {
			return nil, err
		}
	}
	doc, err := office.OpenFromBytes(data, format)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", format, err)
	}
	defer doc.Close()
	actual, err := doc.Format()
	if err != nil {
		return nil, err
	}
	if actual != format {
		return nil, fmt.Errorf("expected %s, detected %s", format, actual)
	}
	text, err := doc.PlainText()
	if err != nil {
		return nil, fmt.Errorf("extract %s text: %w", format, err)
	}
	markdown, err := doc.ToMarkdown()
	if err != nil {
		return nil, fmt.Errorf("extract %s markdown: %w", format, err)
	}
	if len(text) > maxExpandedBytes || len(markdown) > maxExpandedBytes-len(text) {
		return nil, fmt.Errorf("Office cumulative output size exceeds limit")
	}
	return &Result{Format: actual, Text: text, Markdown: markdown, Backend: OfficeOxide}, nil
}
