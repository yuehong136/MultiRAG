package models

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strings"
)

func mediaRequest(client *http.Client, base map[string]string, suffix string, api *APIConfig, contentType string, body io.Reader, model string) (*http.Response, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, fmt.Errorf("media API key is required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("media endpoint suffix is missing")
	}
	endpoint, err := resolveModelBaseURL(base, api.Region)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(api), http.MethodPost, joinModelURL(endpoint, suffix), body)
	if err != nil {
		return nil, fmt.Errorf("create media request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	req.Header.Set("Content-Type", contentType)
	if model != "" {
		req.Header.Set("model", model)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("media request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("media request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}
func mediaJSONRequest(client *http.Client, base map[string]string, suffix string, api *APIConfig, body map[string]any, model string) (*http.Response, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("invalid media request JSON")
	}
	return mediaRequest(client, base, suffix, api, "application/json", bytes.NewReader(raw), model)
}
func readMediaBody(reader io.Reader, limit int) ([]byte, error) {
	raw, err := io.ReadAll(io.LimitReader(reader, int64(limit)+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > limit {
		return nil, fmt.Errorf("media response exceeds limit")
	}
	return raw, nil
}
func readSpeechBytes(resp *http.Response, format string) (*TTSResponse, error) {
	typ, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || (!strings.HasPrefix(typ, "audio/") && typ != "application/octet-stream") {
		return nil, fmt.Errorf("provider did not return audio")
	}
	expected := map[string]string{"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac", "audio/opus": "opus", "audio/aac": "aac"}[typ]
	if expected != "" && expected != format {
		return nil, fmt.Errorf("provider audio MIME does not match requested format")
	}
	audio, err := readMediaBody(resp.Body, MaxMediaBytes)
	if err != nil {
		return nil, err
	}
	result := &TTSResponse{Audio: audio, Format: format}
	if err := ValidateTTSResponse(result); err != nil {
		return nil, err
	}
	return result, nil
}

// scanMediaSSE requires complete event framing. A partial final frame is not EOF success.
func scanMediaSSE(resp *http.Response, consume func(string, []byte) error) error {
	typ, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || typ != "text/event-stream" {
		return fmt.Errorf("provider did not return SSE")
	}
	reader := &io.LimitedReader{R: resp.Body, N: 4*MaxMediaBytes + MaxMediaParamsBytes + 1}
	scanner := bufio.NewScanner(reader)
	limit := 2*MaxMediaBytes + MaxMediaParamsBytes
	scanner.Buffer(make([]byte, 64*1024), limit)
	event := ""
	var data strings.Builder
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if data.Len() > 0 {
				if err := consume(event, []byte(data.String())); err != nil {
					return err
				}
			}
			data.Reset()
			event = ""
		} else if strings.HasPrefix(line, "event:") {
			event = strings.TrimSpace(line[6:])
		} else if strings.HasPrefix(line, "data:") {
			part := strings.TrimPrefix(line[5:], " ")
			if data.Len()+len(part)+1 > limit {
				return fmt.Errorf("media SSE frame exceeds limit")
			}
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.WriteString(part)
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if reader.N == 0 {
		return fmt.Errorf("media SSE exceeds limit")
	}
	if data.Len() > 0 || event != "" {
		return fmt.Errorf("incomplete media SSE frame: %w", io.ErrUnexpectedEOF)
	}
	return nil
}
func speechInput(name *string, text string, config *TTSConfig) (map[string]any, string, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, "", fmt.Errorf("model name is required")
	}
	if err := ValidateSpeechText(text); err != nil {
		return nil, "", err
	}
	if err := ValidateTTSConfig(config); err != nil {
		return nil, "", err
	}
	var params map[string]any
	if config != nil {
		params = config.Params
	}
	body, err := CloneMediaParams(params)
	if err != nil {
		return nil, "", err
	}
	format, err := SpeechFormat(config)
	return body, format, err
}
