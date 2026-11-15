//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package models

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	neturl "net/url"
	"regexp"
	"strings"
	"time"
)

type PaddleOCRModel struct {
	baseModel BaseModel
}

func NewPaddleOCRModel(baseURL map[string]string, urlSuffix URLSuffix) *PaddleOCRModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &PaddleOCRModel{
		baseModel: BaseModel{
			BaseURL:          baseURL,
			URLSuffix:        urlSuffix,
			AllowEmptyAPIKey: true,
			httpClient:       NewDriverHTTPClient(),
		},
	}
}

func (p PaddleOCRModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewPaddleOCRModel(baseURL, p.baseModel.URLSuffix)
}

func (p *PaddleOCRModel) Name() string {
	return "paddle_ocr.net"
}

func (p *PaddleOCRModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", p.Name())
}

type paddleSubmitResponse struct {
	Data struct {
		JobId string `json:"jobId"`
	} `json:"data"`
}

type paddlePollResponse struct {
	Data struct {
		State     string `json:"state"`
		ErrorMsg  string `json:"errorMsg"`
		ResultUrl struct {
			JsonUrl string `json:"jsonUrl"`
		} `json:"resultUrl"`
	} `json:"data"`
}

type paddleJsonlLine struct {
	ErrorCode *int `json:"errorCode"`
	Result    struct {
		LayoutParsingResults []struct {
			Markdown struct {
				Text *string `json:"text"`
			} `json:"markdown"`
			PrunedResult struct {
				ParsingResList []struct {
					Content *string `json:"block_content"`
				} `json:"parsing_res_list"`
			} `json:"prunedResult"`
		} `json:"layoutParsingResults"`
		OCRResults []struct {
			PrunedResult struct {
				Texts []*string `json:"rec_texts"`
			} `json:"prunedResult"`
		} `json:"ocrResults"`
	} `json:"result"`
}

func (p *PaddleOCRModel) OCRFile(modelName *string, content []byte, fileURL *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := p.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if (content == nil || len(content) == 0) && (fileURL == nil || *fileURL == "") {
		return nil, fmt.Errorf("content and fileURL cannot be both empty")
	}
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if len(content) > 20*1024*1024 {
		return nil, fmt.Errorf("OCR content exceeds limit")
	}
	if fileURL != nil && *fileURL != "" {
		parsed, err := neturl.Parse(*fileURL)
		if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.Fragment != "" {
			return nil, fmt.Errorf("invalid OCR input URL")
		}
	}

	resolvedBaseURL, err := p.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, p.baseModel.URLSuffix.OCR)

	optionalPayload := map[string]bool{
		"useDocOrientationClassify": false,
		"useDocUnwarping":           false,
		"useChartRecognition":       false,
	}
	optBytes, _ := json.Marshal(optionalPayload)

	// One generous deadline bounds the whole OCR operation (submit + poll +
	// result download), so the poll loop below can no longer spin forever.
	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	var req *http.Request

	if fileURL != nil && strings.HasPrefix(*fileURL, "http") {
		reqData := map[string]interface{}{
			"fileUrl":         *fileURL,
			"model":           *modelName,
			"optionalPayload": optionalPayload,
		}
		jsonData, err := json.Marshal(reqData)
		if err != nil {
			return nil, fmt.Errorf("failed to marshal json: %w", err)
		}
		req, err = http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
		req.Header.Set("Content-Type", "application/json")
	} else {
		body := &bytes.Buffer{}
		writer := multipart.NewWriter(body)

		_ = writer.WriteField("model", *modelName)
		_ = writer.WriteField("optionalPayload", string(optBytes))

		part, err := writer.CreateFormFile("file", "document.pdf")
		if err != nil {
			return nil, fmt.Errorf("failed to create form file: %w", err)
		}
		part.Write(content)
		writer.Close()

		req, err = http.NewRequestWithContext(ctx, "POST", url, body)
		req.Header.Set("Content-Type", writer.FormDataContentType())
	}

	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := p.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to submit job: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("submit job failed with status %d", resp.StatusCode)
	}

	var submitResp paddleSubmitResponse
	if err := json.Unmarshal(respBody, &submitResp); err != nil {
		return nil, fmt.Errorf("failed to parse submit response: %w", err)
	}

	jobId := submitResp.Data.JobId
	if !regexp.MustCompile(`^[a-zA-Z0-9_-]+$`).MatchString(jobId) {
		return nil, fmt.Errorf("failed to get jobId from response")
	}

	pollUrl := fmt.Sprintf("%s/%s", url, jobId)
	var jsonlUrl string

	for {
		select {
		case <-time.After(3 * time.Second):
		case <-ctx.Done():
			return nil, ctx.Err()
		}

		pollReq, _ := http.NewRequestWithContext(ctx, "GET", pollUrl, nil)
		if auth := BearerAuth(apiConfig); auth != "" {
			pollReq.Header.Set("Authorization", auth)
		}

		pollResp, err := p.baseModel.httpClient.Do(pollReq)
		if err != nil {
			return nil, fmt.Errorf("failed to poll job status: %w", err)
		}

		pollBody, _ := io.ReadAll(pollResp.Body)
		pollResp.Body.Close()

		if pollResp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("poll job failed with status %d", pollResp.StatusCode)
		}

		var pollData paddlePollResponse
		if err = json.Unmarshal(pollBody, &pollData); err != nil {
			return nil, fmt.Errorf("failed to parse poll response: %w", err)
		}

		// end if 'done' or 'failed'
		state := pollData.Data.State
		if state == "done" {
			jsonlUrl = pollData.Data.ResultUrl.JsonUrl
			break
		} else if state == "failed" {
			return nil, fmt.Errorf("ocr job failed on server")
		}
		if state != "pending" && state != "running" && state != "queued" && state != "processing" {
			return nil, fmt.Errorf("invalid OCR job state")
		}
	}

	if jsonlUrl == "" {
		return nil, fmt.Errorf("job done but jsonl url is empty")
	}

	baseOrigin, _ := neturl.Parse(url)
	validateResult := func(target *neturl.URL) error {
		if target == nil || target.Hostname() == "" || (target.Scheme != "http" && target.Scheme != "https") || target.User != nil || target.Fragment != "" {
			return fmt.Errorf("invalid OCR result URL")
		}
		if target.Scheme == baseOrigin.Scheme && target.Host == baseOrigin.Host {
			return nil
		}
		if target.Scheme != "https" {
			return fmt.Errorf("unsafe OCR result URL")
		}
		addresses := []net.IP{}
		if address := net.ParseIP(target.Hostname()); address != nil {
			addresses = append(addresses, address)
		} else {
			resolved, err := net.DefaultResolver.LookupIPAddr(ctx, target.Hostname())
			if err != nil {
				return fmt.Errorf("invalid OCR result address")
			}
			for _, address := range resolved {
				addresses = append(addresses, address.IP)
			}
		}
		if len(addresses) == 0 {
			return fmt.Errorf("invalid OCR result address")
		}
		for _, address := range addresses {
			if !address.IsGlobalUnicast() || address.IsPrivate() || address.IsLoopback() || address.IsLinkLocalUnicast() || address.To4() != nil && address.To4()[0] == 100 && address.To4()[1] >= 64 && address.To4()[1] <= 127 {
				return fmt.Errorf("unsafe OCR result address")
			}
		}
		return nil
	}
	resultURL, err := neturl.Parse(jsonlUrl)
	if err != nil {
		return nil, fmt.Errorf("invalid OCR result URL")
	}
	if err := validateResult(resultURL); err != nil {
		return nil, err
	}
	resReq, err := http.NewRequestWithContext(ctx, "GET", jsonlUrl, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request for jsonl: %w", err)
	}

	downloadClient := *p.baseModel.httpClient
	downloadClient.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return fmt.Errorf("OCR result redirect limit")
		}
		req.Header.Del("Authorization")
		req.Header.Del("Cookie")
		return validateResult(req.URL)
	}
	resResp, err := downloadClient.Do(resReq)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, fmt.Errorf("failed to download jsonl result")
	}
	defer resResp.Body.Close()

	if resResp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to download jsonl, status: %d", resResp.StatusCode)
	}

	var fullMarkdown strings.Builder
	limited := io.LimitReader(resResp.Body, 20*1024*1024+1)
	scanner := bufio.NewScanner(limited)
	scanner.Buffer(make([]byte, 64*1024), 20*1024*1024)
	lineCount := 0

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}

		var lineData *paddleJsonlLine
		if err := json.Unmarshal([]byte(line), &lineData); err != nil || lineData == nil {
			return nil, fmt.Errorf("invalid OCR result JSONL")
		}
		if lineData.ErrorCode != nil && *lineData.ErrorCode != 0 {
			return nil, fmt.Errorf("OCR result reports failure")
		}
		found := false

		for _, layoutRes := range lineData.Result.LayoutParsingResults {
			if layoutRes.Markdown.Text != nil {
				fullMarkdown.WriteString(*layoutRes.Markdown.Text)
				fullMarkdown.WriteString("\n\n")
				found = true
			} else {
				for _, block := range layoutRes.PrunedResult.ParsingResList {
					if block.Content == nil {
						return nil, fmt.Errorf("invalid OCR layout block")
					}
					fullMarkdown.WriteString(*block.Content)
					fullMarkdown.WriteString("\n\n")
					found = true
				}
			}
		}
		for _, result := range lineData.Result.OCRResults {
			var texts []string
			for _, text := range result.PrunedResult.Texts {
				if text == nil {
					return nil, fmt.Errorf("invalid OCR text")
				}
				texts = append(texts, *text)
			}
			if len(texts) > 0 {
				fullMarkdown.WriteString(strings.Join(texts, "\n"))
				fullMarkdown.WriteString("\n\n")
				found = true
			}
		}
		if !found {
			return nil, fmt.Errorf("OCR result contains no text fields")
		}
		lineCount++
	}

	if err = scanner.Err(); err != nil {
		return nil, fmt.Errorf("error reading jsonl: %w", err)
	}
	if lineCount == 0 {
		return nil, fmt.Errorf("OCR result is empty")
	}

	extractedText := strings.TrimSpace(fullMarkdown.String())

	return &OCRFileResponse{Text: &extractedText}, nil
}

func (p *PaddleOCRModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) CheckConnection(apiConfig *APIConfig) error {
	return fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}

func (p *PaddleOCRModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", p.Name())
}
