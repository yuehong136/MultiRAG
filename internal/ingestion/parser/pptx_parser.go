package parser

import (
	"bytes"
	"encoding/xml"
	"fmt"
	"io"
	"path"
	"strings"
)

type relationship struct {
	ID     string `xml:"Id,attr"`
	Target string `xml:"Target,attr"`
	Type   string `xml:"Type,attr"`
	Mode   string `xml:"TargetMode,attr"`
}
type relationships struct {
	XMLName xml.Name       `xml:"Relationships"`
	Items   []relationship `xml:"Relationship"`
}

func resolvePart(a *officeArchive, rels relationships, id, base, kind string) (string, error) {
	for _, r := range rels.Items {
		if r.ID != id {
			continue
		}
		if r.Mode == "External" || !strings.HasSuffix(r.Type, "/"+kind) {
			return "", fmt.Errorf("invalid %s relationship", kind)
		}
		target := path.Clean(path.Join(base, r.Target))
		if strings.HasPrefix(r.Target, "/") {
			target = strings.TrimPrefix(path.Clean(r.Target), "/")
		}
		if a.files[target] == nil {
			return "", fmt.Errorf("missing %s relationship target", kind)
		}
		return target, nil
	}
	return "", fmt.Errorf("missing %s relationship %q", kind, id)
}
func parsePPTX(a *officeArchive) (*Result, error) {
	var presentation struct {
		XMLName xml.Name `xml:"presentation"`
		Slides  []struct {
			ID string `xml:"http://schemas.openxmlformats.org/officeDocument/2006/relationships id,attr"`
		} `xml:"sldIdLst>sldId"`
	}
	if err := a.decode("ppt/presentation.xml", &presentation); err != nil {
		return nil, err
	}
	var rels relationships
	if err := a.decode("ppt/_rels/presentation.xml.rels", &rels); err != nil {
		return nil, err
	}
	var plain, markdown strings.Builder
	count := 0
	for _, s := range presentation.Slides {
		name, err := resolvePart(a, rels, s.ID, "ppt", "slide")
		if err != nil {
			return nil, err
		}
		data, err := a.read(name)
		if err != nil {
			return nil, err
		}
		text, err := drawingText(data)
		if err != nil {
			return nil, err
		}
		if count > 0 {
			if err := a.emit(&plain, "\n\n"); err != nil {
				return nil, err
			}
			if err := a.emit(&markdown, "\n\n---\n\n"); err != nil {
				return nil, err
			}
		}
		if err := a.emit(&plain, text); err != nil {
			return nil, err
		}
		if err := a.emit(&markdown, text); err != nil {
			return nil, err
		}
		count++
	}
	return &Result{Text: plain.String(), Markdown: markdown.String()}, nil
}
func drawingText(data []byte) (string, error) {
	var lines []string
	var b strings.Builder
	inText := false
	d := xml.NewDecoder(bytes.NewReader(data))
	root := false
	for {
		token, err := d.Token()
		if err != nil {
			if err == io.EOF {
				break
			}
			return "", err
		}
		switch t := token.(type) {
		case xml.StartElement:
			if !root {
				if t.Name.Local != "sld" {
					return "", fmt.Errorf("invalid PPTX slide root %q", t.Name.Local)
				}
				root = true
			}
			if t.Name.Local == "t" {
				inText = true
			}
			if t.Name.Local == "br" {
				b.WriteByte('\n')
			}
		case xml.CharData:
			if inText {
				b.Write(t)
			}
		case xml.EndElement:
			if t.Name.Local == "t" {
				inText = false
			}
			if t.Name.Local == "p" {
				lines = append(lines, b.String())
				b.Reset()
			}
		}
	}
	if !root {
		return "", fmt.Errorf("missing PPTX slide root")
	}
	return strings.Join(lines, "\n"), nil
}
