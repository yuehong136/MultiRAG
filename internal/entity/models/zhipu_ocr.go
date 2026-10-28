package models

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
)

func (z *ZhipuAIModel) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return z.ocr(name, content, "", api)
}
func (z *ZhipuAIModel) OCRURL(name *string, fileURL string, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return z.ocr(name, nil, fileURL, api)
}
func (z *ZhipuAIModel) ocr(name *string, content []byte, fileURL string, api *APIConfig) (*OCRResponse, error) {
	if err := ocrInputBody(name, content, fileURL); err != nil {
		return nil, err
	}
	if fileURL == "" {
		typ := http.DetectContentType(content)
		if bytes.HasPrefix(content, []byte("%PDF")) {
			typ = "application/pdf"
		}
		fileURL = "data:" + typ + ";base64," + base64.StdEncoding.EncodeToString(content)
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	client := *z.httpClient
	client.CheckRedirect = rejectModelRedirect
	resp, err := mediaJSONRequest(&client, z.BaseURL, z.URLSuffix.OCR, local, map[string]any{"model": *name, "file": fileURL}, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Text  *string         `json:"md_results"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		if ctxErr := requestContext(local).Err(); ctxErr != nil {
			return nil, ctxErr
		}
		return nil, fmt.Errorf("invalid Zhipu OCR response")
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("Zhipu OCR provider error")
	}
	response := &OCRResponse{Text: result.Text}
	if err := ValidateOCRResponse(response); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return response, nil
}

var _ OCRDriver = (*ZhipuAIModel)(nil)
var _ OCRURLDriver = (*ZhipuAIModel)(nil)
