package io

import (
	"errors"
	"testing"
)

func TestPDFWriterReportsMissingFont(t *testing.T) {
	payload, err := WritePDF("actual document", PDFOptions{FontSize: 12, AddPageNumbers: true})
	if payload != nil || !errors.Is(err, ErrPDFFontNotConfigured) {
		t.Fatalf("unconfigured font became fake document: %d bytes %v", len(payload), err)
	}
}
