// Package parser provides bounded local document text extraction.
package parser

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"
)

const MaxDocumentBytes = 32 << 20
const Builtin = "builtin"
const OfficeOxide = "office_oxide"

var ErrUnsupported = errors.New("unsupported local parser")

// Result carries actual extraction output. Parsers never print to stdout.
type Result struct {
	Format   string `json:"format"`
	Text     string `json:"text"`
	Markdown string `json:"markdown"`
	Backend  string `json:"backend"`
}

// Parse selects a real backend from config. No fallback silently ignores options.
func Parse(filename string, data []byte, config map[string]string) (*Result, error) {
	if len(data) == 0 || len(data) > MaxDocumentBytes {
		return nil, fmt.Errorf("document size must be 1..%d bytes", MaxDocumentBytes)
	}
	for key := range config {
		if key != "lib_type" {
			return nil, fmt.Errorf("unknown parser configuration %q", key)
		}
	}
	backend := config["lib_type"]
	if backend == "" {
		return nil, fmt.Errorf("missing lib_type config")
	}
	format := strings.ToLower(strings.TrimPrefix(filepath.Ext(filename), "."))
	switch backend {
	case Builtin:
		archive, err := openOfficeArchive(data)
		if err != nil {
			return nil, err
		}
		var result *Result
		switch format {
		case "docx":
			result, err = parseDOCX(archive)
		case "pptx":
			result, err = parsePPTX(archive)
		case "xlsx":
			result, err = parseXLSX(archive)
		default:
			return nil, fmt.Errorf("%w: builtin %s", ErrUnsupported, format)
		}
		if err != nil {
			return nil, err
		}
		result.Format, result.Backend = format, backend
		return result, nil
	case OfficeOxide:
		switch format {
		case "doc", "docx", "xls", "ppt", "pptx", "xlsx":
			return parseOfficeOxide(format, data)
		default:
			return nil, fmt.Errorf("%w: office_oxide %s", ErrUnsupported, format)
		}
	default:
		return nil, fmt.Errorf("%w: library %q", ErrUnsupported, backend)
	}
}
