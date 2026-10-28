package models

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
)

func ocrInputBody(name *string, content []byte, fileURL string) error {
	if name == nil || strings.TrimSpace(*name) == "" {
		return fmt.Errorf("OCR model required")
	}
	return ValidateParseInput(content, fileURL)
}

func readOCRJSON(resp *http.Response, target any) error {
	raw, err := readMediaBody(resp.Body, MaxMediaBytes)
	if err != nil {
		return err
	}
	var envelope struct {
		Error     json.RawMessage `json:"error"`
		Code      *int            `json:"code"`
		ErrorCode *int            `json:"error_code"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return fmt.Errorf("invalid OCR response")
	}
	if len(envelope.Error) > 0 && string(envelope.Error) != "null" || envelope.Code != nil && *envelope.Code != 0 || envelope.ErrorCode != nil && *envelope.ErrorCode != 0 {
		return fmt.Errorf("OCR provider error")
	}
	if err := json.Unmarshal(raw, target); err != nil {
		return fmt.Errorf("invalid OCR result")
	}
	return nil
}

func ocrText(parts []string) (*OCRResponse, error) {
	text := strings.TrimSpace(strings.Join(parts, "\n\n"))
	if text == "" || len(text) > MaxMediaBytes {
		return nil, fmt.Errorf("empty or oversized OCR result")
	}
	return &OCRResponse{Text: &text}, nil
}

func (b *BaiduModel) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return b.ocr(name, content, "", api)
}
func (b *BaiduModel) OCRURL(name *string, fileURL string, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return b.ocr(name, nil, fileURL, api)
}
func (b *BaiduModel) ocr(name *string, content []byte, fileURL string, api *APIConfig) (*OCRResponse, error) {
	if err := ocrInputBody(name, content, fileURL); err != nil {
		return nil, err
	}
	input, typ := fileURL, 1
	if fileURL == "" {
		input = base64.StdEncoding.EncodeToString(content)
		if bytes.HasPrefix(content, []byte("%PDF-")) {
			typ = 0
		}
	} else {
		parsed, _ := url.Parse(fileURL)
		if strings.HasSuffix(strings.ToLower(parsed.Path), ".pdf") {
			typ = 0
		}
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(b.httpClient, b.BaseURL, b.URLSuffix.OCR, local, map[string]any{"model": *name, "file": input, "fileType": typ}, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Result *struct {
			Pages []struct {
				Markdown *struct {
					Text *string `json:"text"`
				} `json:"markdown"`
			} `json:"layoutParsingResults"`
		} `json:"result"`
	}
	if err := readOCRJSON(resp, &result); err != nil {
		return nil, err
	}
	if result.Result == nil {
		return nil, fmt.Errorf("missing Baidu OCR result")
	}
	parts := make([]string, 0, len(result.Result.Pages))
	for _, page := range result.Result.Pages {
		if page.Markdown == nil || page.Markdown.Text == nil {
			return nil, fmt.Errorf("missing Baidu OCR page")
		}
		parts = append(parts, *page.Markdown.Text)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return ocrText(parts)
}

func (m *MistralModel) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return m.ocr(name, content, "", api)
}
func (m *MistralModel) OCRURL(name *string, fileURL string, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return m.ocr(name, nil, fileURL, api)
}
func (m *MistralModel) ocr(name *string, content []byte, fileURL string, api *APIConfig) (*OCRResponse, error) {
	if err := ocrInputBody(name, content, fileURL); err != nil {
		return nil, err
	}
	if fileURL == "" {
		fileURL = "data:" + http.DetectContentType(content) + ";base64," + base64.StdEncoding.EncodeToString(content)
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(m.httpClient, m.BaseURL, m.URLSuffix.OCR, local, map[string]any{"model": *name, "document": map[string]string{"type": "document_url", "document_url": fileURL}}, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Pages []struct {
			Index    *int    `json:"index"`
			Markdown *string `json:"markdown"`
		} `json:"pages"`
	}
	if err := readOCRJSON(resp, &result); err != nil {
		return nil, err
	}
	seen := map[int]bool{}
	for _, page := range result.Pages {
		if page.Index == nil || *page.Index < 0 || seen[*page.Index] || page.Markdown == nil {
			return nil, fmt.Errorf("invalid Mistral OCR page")
		}
		seen[*page.Index] = true
	}
	sort.SliceStable(result.Pages, func(i, j int) bool { return *result.Pages[i].Index < *result.Pages[j].Index })
	parts := make([]string, 0, len(result.Pages))
	for _, page := range result.Pages {
		parts = append(parts, *page.Markdown)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return ocrText(parts)
}

var _ OCRDriver = (*BaiduModel)(nil)
var _ OCRURLDriver = (*BaiduModel)(nil)
var _ OCRDriver = (*MistralModel)(nil)
var _ OCRURLDriver = (*MistralModel)(nil)
