package models

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"strings"
)

// PaddleOCRLocalModel sends already-authorized bytes to a configured local server.
// It deliberately has no OCRURLDriver: local file fetching is not authorized here.
type PaddleOCRLocalModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewPaddleOCRLocalModel(urls map[string]string, suffix URLSuffix) *PaddleOCRLocalModel {
	return &PaddleOCRLocalModel{NewDummyModel(maps.Clone(urls), suffix), &http.Client{Transport: openAIChatTransport, CheckRedirect: rejectModelRedirect}}
}
func (p *PaddleOCRLocalModel) Name() string { return "paddleocr_local" }

func (p *PaddleOCRLocalModel) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	if err := ocrInputBody(name, content, ""); err != nil {
		return nil, err
	}
	typ := 1
	if bytes.HasPrefix(content, []byte("%PDF")) {
		typ = 0
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(p.httpClient, p.BaseURL, p.URLSuffix.OCR, local, map[string]any{"file": base64.StdEncoding.EncodeToString(content), "fileType": typ}, "", mediaHTTPOptions{optionalAPIKey: true})
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		ErrorCode *int            `json:"errorCode"`
		Error     json.RawMessage `json:"error"`
		Result    *struct {
			Pages *[]struct {
				Markdown *struct {
					Text *string `json:"text"`
				} `json:"markdown"`
			} `json:"layoutParsingResults"`
		} `json:"result"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		if ctxErr := requestContext(local).Err(); ctxErr != nil {
			return nil, ctxErr
		}
		return nil, fmt.Errorf("invalid local PaddleOCR response")
	}
	if result.ErrorCode == nil || *result.ErrorCode != 0 || len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("local PaddleOCR provider error")
	}
	if result.Result == nil || result.Result.Pages == nil || len(*result.Result.Pages) == 0 {
		return nil, fmt.Errorf("missing local PaddleOCR pages")
	}
	parts := make([]string, 0, len(*result.Result.Pages))
	for _, page := range *result.Result.Pages {
		if page.Markdown == nil || page.Markdown.Text == nil {
			return nil, fmt.Errorf("missing local PaddleOCR page text")
		}
		parts = append(parts, *page.Markdown.Text)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	text := strings.TrimSpace(strings.Join(parts, "\n\n"))
	response := &OCRResponse{Text: &text}
	if err := ValidateOCRResponse(response); err != nil {
		return nil, err
	}
	return response, nil
}

var _ OCRDriver = (*PaddleOCRLocalModel)(nil)
