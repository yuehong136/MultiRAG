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
func parseXLSX(a officeArchive) (*Result, error) {
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
	if a["xl/sharedStrings.xml"] != nil {
		if err := a.decode("xl/sharedStrings.xml", &shared); err != nil {
			return nil, err
		}
	}
	var plain, markdown []string
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
		var lines []string
		for _, row := range content.Rows {
			var cells []string
			for _, cell := range row.Cells {
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
				// Include coordinates so sparse rows and formulas' cached values remain inspectable.
				cells = append(cells, cell.Ref+"="+value)
			}
			lines = append(lines, strings.Join(cells, "\t"))
		}
		text := strings.Join(lines, "\n")
		plain = append(plain, sheet.Name+"\n"+text)
		markdown = append(markdown, "## "+sheet.Name+"\n\n"+text)
	}
	return &Result{Text: strings.Join(plain, "\n\n"), Markdown: strings.Join(markdown, "\n\n")}, nil
}
