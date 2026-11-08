package parser

import (
	"encoding/xml"
	"fmt"
	"strconv"
	"strings"
)

type richString struct {
	Text string `xml:"t"`
	Runs []struct {
		Text string `xml:"t"`
	} `xml:"r"`
}

func (s richString) text() string {
	var b strings.Builder
	b.WriteString(s.Text)
	for _, r := range s.Runs {
		b.WriteString(r.Text)
	}
	return b.String()
}
func parseXLSX(a *officeArchive) (*Result, error) {
	var workbook struct {
		XMLName xml.Name `xml:"workbook"`
		Sheets  []struct {
			Name string `xml:"name,attr"`
			ID   string `xml:"http://schemas.openxmlformats.org/officeDocument/2006/relationships id,attr"`
		} `xml:"sheets>sheet"`
	}
	if err := a.decode("xl/workbook.xml", &workbook); err != nil {
		return nil, err
	}
	var rels relationships
	if err := a.decode("xl/_rels/workbook.xml.rels", &rels); err != nil {
		return nil, err
	}
	var shared struct {
		XMLName xml.Name     `xml:"sst"`
		Items   []richString `xml:"si"`
	}
	if a.files["xl/sharedStrings.xml"] != nil {
		if err := a.decode("xl/sharedStrings.xml", &shared); err != nil {
			return nil, err
		}
	}
	var plain, markdown strings.Builder
	sheetCount := 0
	for _, sheet := range workbook.Sheets {
		target, err := resolvePart(a, rels, sheet.ID, "xl", "worksheet")
		if err != nil {
			return nil, err
		}
		var content struct {
			XMLName xml.Name `xml:"worksheet"`
			Rows    []struct {
				Cells []struct {
					Ref    string     `xml:"r,attr"`
					Type   string     `xml:"t,attr"`
					Value  string     `xml:"v"`
					Inline richString `xml:"is"`
				} `xml:"c"`
			} `xml:"sheetData>row"`
		}
		if err := a.decode(target, &content); err != nil {
			return nil, err
		}
		if sheetCount > 0 {
			if err := a.emit(&plain, "\n\n"); err != nil {
				return nil, err
			}
			if err := a.emit(&markdown, "\n\n"); err != nil {
				return nil, err
			}
		}
		if err := a.emit(&plain, sheet.Name, "\n"); err != nil {
			return nil, err
		}
		if err := a.emit(&markdown, "## ", sheet.Name, "\n\n"); err != nil {
			return nil, err
		}
		for rowIndex, row := range content.Rows {
			if rowIndex > 0 {
				if err := a.emit(&plain, "\n"); err != nil {
					return nil, err
				}
				if err := a.emit(&markdown, "\n"); err != nil {
					return nil, err
				}
			}
			for cellIndex, cell := range row.Cells {
				value := cell.Value
				switch cell.Type {
				case "s":
					i, err := strconv.Atoi(value)
					if err != nil || i < 0 || i >= len(shared.Items) {
						return nil, fmt.Errorf("invalid XLSX shared string index")
					}
					value = shared.Items[i].text()
				case "inlineStr":
					value = cell.Inline.text()
				}
				if cellIndex > 0 {
					if err := a.emit(&plain, "\t"); err != nil {
						return nil, err
					}
					if err := a.emit(&markdown, "\t"); err != nil {
						return nil, err
					}
				}
				if err := a.emit(&plain, cell.Ref, "=", value); err != nil {
					return nil, err
				}
				if err := a.emit(&markdown, cell.Ref, "=", value); err != nil {
					return nil, err
				}
			}
		}
		sheetCount++
	}
	return &Result{Text: plain.String(), Markdown: markdown.String()}, nil
}
