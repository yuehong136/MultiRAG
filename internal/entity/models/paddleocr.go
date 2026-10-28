package models

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const paddleOCRBaseURL = "https://paddleocr.aistudio-app.com/api"

type PaddleOCRModel struct {
	*DummyModel
	httpClient   *http.Client
	pollInterval time.Duration
}

func NewPaddleOCRModel(base map[string]string, suffix URLSuffix) *PaddleOCRModel {
	return &PaddleOCRModel{DummyModel: NewDummyModel(base, suffix), httpClient: &http.Client{Transport: openAIChatTransport}, pollInterval: time.Second}
}
func (p *PaddleOCRModel) Name() string { return "paddleocr" }

// jobConfig unwraps only this provider's JSON credential contract. Explicit SQL
// instance endpoints take priority over a key payload's endpoint.
func (p *PaddleOCRModel) jobConfig(name string, api *APIConfig) (string, *APIConfig, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return "", nil, fmt.Errorf("PaddleOCR access token required")
	}
	base, err := resolveModelBaseURL(p.BaseURL, api.Region)
	if err != nil {
		return "", nil, err
	}
	if p.URLSuffix.OCR == "" {
		return "", nil, fmt.Errorf("PaddleOCR endpoint required")
	}
	endpoint := joinModelURL(base, p.URLSuffix.OCR)
	token := strings.TrimSpace(*api.APIKey)
	if strings.HasPrefix(token, "[") || strings.HasPrefix(token, `"`) {
		return "", nil, fmt.Errorf("PaddleOCR configuration must be an object")
	}
	if strings.HasPrefix(token, "{") {
		var raw map[string]json.RawMessage
		if err := json.Unmarshal([]byte(token), &raw); err != nil || raw == nil {
			return "", nil, fmt.Errorf("invalid PaddleOCR configuration")
		}
		if nested, ok := raw["api_key"]; ok {
			if err := json.Unmarshal(nested, &raw); err != nil || raw == nil {
				return "", nil, fmt.Errorf("invalid PaddleOCR api_key configuration")
			}
		}
		get := func(key string) (string, error) {
			for _, key := range []string{key, strings.ToUpper(key)} {
				if value, ok := raw[key]; ok {
					var text string
					if err := json.Unmarshal(value, &text); err != nil {
						return "", fmt.Errorf("invalid PaddleOCR configuration field")
					}
					return strings.TrimSpace(text), nil
				}
			}
			return "", nil
		}
		token, err = get("paddleocr_access_token")
		if err != nil || token == "" {
			return "", nil, fmt.Errorf("PaddleOCR access token required in configuration")
		}
		algorithm, err := get("paddleocr_algorithm")
		if err != nil {
			return "", nil, err
		}
		if algorithm != "" && algorithm != name {
			return "", nil, fmt.Errorf("PaddleOCR algorithm conflicts with selected model")
		}
		keyBase, err := get("paddleocr_base_url")
		if err != nil {
			return "", nil, err
		}
		keyURL, err := get("paddleocr_api_url")
		if err != nil {
			return "", nil, err
		}
		if strings.TrimRight(base, "/") == paddleOCRBaseURL {
			if keyBase != "" {
				endpoint = joinModelURL(keyBase, p.URLSuffix.OCR)
			} else if keyURL != "" {
				endpoint = keyURL
			}
		}
	}
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.Scheme != "http" && parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", nil, fmt.Errorf("invalid PaddleOCR job endpoint")
	}
	resolved := *api
	resolved.APIKey = &token
	return strings.TrimRight(endpoint, "/"), &resolved, nil
}

func (p *PaddleOCRModel) jobJSON(method, endpoint string, api *APIConfig, contentType string, body io.Reader, target any) error {
	req, err := http.NewRequestWithContext(requestContext(api), method, endpoint, body)
	if err != nil {
		return fmt.Errorf("invalid PaddleOCR request")
	}
	req.Header.Set("Authorization", "Bearer "+*api.APIKey)
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	client := *p.httpClient
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := client.Do(req)
	if err != nil {
		if err := requestContext(api).Err(); err != nil {
			return err
		}
		return fmt.Errorf("PaddleOCR job request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && !(method == http.MethodPost && resp.StatusCode == http.StatusAccepted) {
		return fmt.Errorf("PaddleOCR job request failed (HTTP %d)", resp.StatusCode)
	}
	raw, err := readMediaBody(resp.Body, MaxMediaBytes)
	if err != nil {
		return err
	}
	var envelope struct {
		ErrorCode *int            `json:"errorCode"`
		Code      *int            `json:"code"`
		Error     json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return fmt.Errorf("invalid PaddleOCR response")
	}
	if envelope.Code != nil && *envelope.Code != 0 || envelope.ErrorCode != nil && *envelope.ErrorCode != 0 || len(envelope.Error) > 0 && string(envelope.Error) != "null" {
		return fmt.Errorf("PaddleOCR provider error")
	}
	if err := json.Unmarshal(raw, target); err != nil {
		return fmt.Errorf("invalid PaddleOCR job result")
	}
	return requestContext(api).Err()
}

func (p *PaddleOCRModel) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return p.ocr(name, content, "", api)
}
func (p *PaddleOCRModel) OCRURL(name *string, fileURL string, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return p.ocr(name, nil, fileURL, api)
}
func (p *PaddleOCRModel) ocr(name *string, content []byte, fileURL string, api *APIConfig) (*OCRResponse, error) {
	if err := ocrInputBody(name, content, fileURL); err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	endpoint, resolved, err := p.jobConfig(*name, local)
	if err != nil {
		return nil, err
	}
	options := map[string]bool{"useDocOrientationClassify": false, "useDocUnwarping": false, "useChartRecognition": false}
	var body bytes.Buffer
	contentType := "application/json"
	if fileURL != "" {
		if err := json.NewEncoder(&body).Encode(map[string]any{"model": *name, "fileUrl": fileURL, "optionalPayload": options}); err != nil {
			return nil, err
		}
	} else {
		w := multipart.NewWriter(&body)
		if err := w.WriteField("model", *name); err != nil {
			return nil, err
		}
		raw, _ := json.Marshal(options)
		if err := w.WriteField("optionalPayload", string(raw)); err != nil {
			return nil, err
		}
		part, err := w.CreateFormFile("file", "document.pdf")
		if err != nil {
			return nil, err
		}
		if _, err := part.Write(content); err != nil {
			return nil, err
		}
		if err := w.Close(); err != nil {
			return nil, err
		}
		contentType = w.FormDataContentType()
	}
	var submit struct {
		Data *struct {
			JobID string `json:"jobId"`
		} `json:"data"`
	}
	if err := p.jobJSON(http.MethodPost, endpoint, resolved, contentType, &body, &submit); err != nil {
		return nil, err
	}
	if submit.Data == nil {
		return nil, fmt.Errorf("missing PaddleOCR job")
	}
	if err := ValidateTaskID(submit.Data.JobID); err != nil {
		return nil, fmt.Errorf("invalid PaddleOCR job ID")
	}
	for attempt := 0; attempt < 120; attempt++ {
		var status struct {
			Data *struct {
				State         string `json:"state"`
				ResultJSONURL string `json:"resultJsonUrl"`
				ResultURL     struct {
					JSONURL string `json:"jsonUrl"`
				} `json:"resultUrl"`
			} `json:"data"`
		}
		if err := p.jobJSON(http.MethodGet, endpoint+"/"+submit.Data.JobID, resolved, "", nil, &status); err != nil {
			return nil, err
		}
		if status.Data == nil {
			return nil, fmt.Errorf("missing PaddleOCR status")
		}
		switch status.Data.State {
		case "done":
			resultURL := status.Data.ResultURL.JSONURL
			if resultURL == "" {
				resultURL = status.Data.ResultJSONURL
			}
			return p.downloadResult(endpoint, resultURL, resolved)
		case "failed":
			return nil, fmt.Errorf("PaddleOCR job failed")
		case "pending", "running":
		default:
			return nil, fmt.Errorf("unknown PaddleOCR job state")
		}
		timer := time.NewTimer(p.pollInterval)
		select {
		case <-requestContext(resolved).Done():
			timer.Stop()
			return nil, requestContext(resolved).Err()
		case <-timer.C:
		}
	}
	return nil, fmt.Errorf("PaddleOCR job polling limit exceeded")
}

func readPaddleJSONL(reader io.Reader) (*OCRResponse, error) {
	limited := &io.LimitedReader{R: reader, N: MaxMediaBytes + 1}
	scanner := bufio.NewScanner(limited)
	scanner.Buffer(make([]byte, 64*1024), MaxMediaBytes+1)
	var parts []string
	for scanner.Scan() {
		raw := bytes.TrimSpace(scanner.Bytes())
		if len(raw) == 0 {
			continue
		}
		var line struct {
			Error     json.RawMessage `json:"error"`
			ErrorCode *int            `json:"errorCode"`
			Code      *int            `json:"code"`
			Result    *struct {
				Layout []struct {
					Markdown *struct {
						Text *string `json:"text"`
					} `json:"markdown"`
					Pruned *struct {
						Blocks *[]struct {
							Content *string `json:"block_content"`
						} `json:"parsing_res_list"`
					} `json:"prunedResult"`
				} `json:"layoutParsingResults"`
				OCR []struct {
					Pruned *struct {
						Texts *[]*string `json:"rec_texts"`
					} `json:"prunedResult"`
				} `json:"ocrResults"`
			} `json:"result"`
		}
		if err := json.Unmarshal(raw, &line); err != nil || line.Result == nil {
			return nil, fmt.Errorf("invalid PaddleOCR JSONL row")
		}
		if line.Code != nil && *line.Code != 0 || line.ErrorCode != nil && *line.ErrorCode != 0 || len(line.Error) > 0 && string(line.Error) != "null" {
			return nil, fmt.Errorf("PaddleOCR JSONL provider error")
		}
		if len(line.Result.Layout) > 0 {
			for _, page := range line.Result.Layout {
				if page.Markdown != nil && page.Markdown.Text != nil {
					parts = append(parts, *page.Markdown.Text)
				} else if page.Pruned != nil && page.Pruned.Blocks != nil {
					for _, block := range *page.Pruned.Blocks {
						if block.Content == nil {
							return nil, fmt.Errorf("missing PaddleOCR block content")
						}
						parts = append(parts, *block.Content)
					}
				} else {
					return nil, fmt.Errorf("missing PaddleOCR layout text")
				}
			}
		} else if len(line.Result.OCR) > 0 {
			for _, page := range line.Result.OCR {
				if page.Pruned == nil || page.Pruned.Texts == nil {
					return nil, fmt.Errorf("missing PaddleOCR recognition text")
				}
				texts := make([]string, 0, len(*page.Pruned.Texts))
				for _, text := range *page.Pruned.Texts {
					if text == nil {
						return nil, fmt.Errorf("invalid PaddleOCR recognition text")
					}
					texts = append(texts, *text)
				}
				parts = append(parts, strings.Join(texts, "\n"))
			}
		} else {
			return nil, fmt.Errorf("missing PaddleOCR page results")
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if limited.N == 0 {
		return nil, fmt.Errorf("PaddleOCR JSONL exceeds limit")
	}
	return ocrText(parts)
}

var _ OCRDriver = (*PaddleOCRModel)(nil)
var _ OCRURLDriver = (*PaddleOCRModel)(nil)
