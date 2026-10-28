package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"strings"
)

func (g *GiteeModel) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	if err := ValidateMediaBytes(content); err != nil {
		return nil, err
	}
	return g.ocrInput(name, content, "", api)
}
func (g *GiteeModel) OCRURL(name *string, imageURL string, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	if err := ValidateOCRURL(imageURL); err != nil {
		return nil, err
	}
	return g.ocrInput(name, nil, imageURL, api)
}
func (g *GiteeModel) ocrInput(name *string, content []byte, imageURL string, api *APIConfig) (*OCRResponse, error) {
	if name == nil || strings.TrimSpace(*name) == "" || api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, fmt.Errorf("gitee OCR model and API key are required")
	}
	if g.URLSuffix.OCR == "" {
		return nil, fmt.Errorf("gitee OCR endpoint is missing")
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	base, err := resolveModelBaseURL(g.BaseURL, local.Region)
	if err != nil {
		return nil, err
	}
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("model", *name); err != nil {
		return nil, err
	}
	if imageURL != "" {
		err = writer.WriteField("image", imageURL)
	} else {
		var part io.Writer
		part, err = writer.CreateFormFile("image", "image")
		if err == nil {
			_, err = part.Write(content)
		}
	}
	if err != nil {
		return nil, err
	}
	if err = writer.Close(); err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(local), http.MethodPost, joinModelURL(base, g.URLSuffix.OCR), &body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	resp, err := g.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("gitee OCR request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gitee OCR failed (HTTP %d)", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, MaxMediaBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > MaxMediaBytes {
		return nil, fmt.Errorf("gitee OCR response exceeds 20 MiB")
	}
	var result struct {
		Text  *string         `json:"text_result"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeCoHereJSON(bytes.NewReader(raw), &result); err != nil {
		return nil, err
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("gitee OCR provider error")
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

var _ OCRDriver = (*GiteeModel)(nil)
var _ OCRURLDriver = (*GiteeModel)(nil)
