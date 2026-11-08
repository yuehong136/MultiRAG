package parser

import (
	"archive/zip"
	"bytes"
	"encoding/xml"
	"fmt"
	"io"
	"path"
	"strings"
)

const maxExpandedBytes = 64 << 20

type officeArchive struct {
	files           map[string]*zip.File
	readRemaining   uint64
	outputRemaining int
}

func openOfficeArchive(data []byte) (*officeArchive, error) {
	r, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("invalid Office archive: %w", err)
	}
	if len(r.File) > 4096 {
		return nil, fmt.Errorf("too many Office archive entries")
	}
	out := &officeArchive{files: make(map[string]*zip.File, len(r.File)), readRemaining: maxExpandedBytes, outputRemaining: maxExpandedBytes}
	var total uint64
	for _, f := range r.File {
		if f.FileInfo().IsDir() {
			continue
		}
		if path.Clean(f.Name) != f.Name || path.IsAbs(f.Name) || len(f.Name) > 512 {
			return nil, fmt.Errorf("invalid archive entry path")
		}
		if _, exists := out.files[f.Name]; exists {
			return nil, fmt.Errorf("duplicate archive entry %q", f.Name)
		}
		if f.UncompressedSize64 > maxExpandedBytes || total > maxExpandedBytes-f.UncompressedSize64 {
			return nil, fmt.Errorf("Office archive expanded size exceeds limit")
		}
		total += f.UncompressedSize64
		out.files[f.Name] = f
	}
	return out, nil
}
func (a *officeArchive) read(name string) ([]byte, error) {
	f := a.files[name]
	if f == nil {
		return nil, fmt.Errorf("missing Office part %q", name)
	}
	if f.UncompressedSize64 > a.readRemaining {
		return nil, fmt.Errorf("Office cumulative read size exceeds limit")
	}
	a.readRemaining -= f.UncompressedSize64
	r, err := f.Open()
	if err != nil {
		return nil, err
	}
	defer r.Close()
	data, err := io.ReadAll(io.LimitReader(r, maxExpandedBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", name, err)
	}
	if len(data) > maxExpandedBytes {
		return nil, fmt.Errorf("Office part size exceeds limit")
	}
	return data, nil
}
func (a *officeArchive) decode(name string, v any) error {
	data, err := a.read(name)
	if err != nil {
		return err
	}
	if err := xml.Unmarshal(data, v); err != nil {
		return fmt.Errorf("decode %s: %w", name, err)
	}
	return nil
}

// emit bounds the combined Text/Markdown output, including repeated references.
func (a *officeArchive) emit(destination *strings.Builder, pieces ...string) error {
	total := 0
	for _, piece := range pieces {
		if len(piece) > a.outputRemaining-total {
			return fmt.Errorf("Office cumulative output size exceeds limit")
		}
		total += len(piece)
	}
	a.outputRemaining -= total
	for _, piece := range pieces {
		destination.WriteString(piece)
	}
	return nil
}
