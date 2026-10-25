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

type mediaHTTPOptions struct {
	optionalAPIKey bool
	accept         string
}

func mediaRequest(client *http.Client, base map[string]string, suffix string, api *APIConfig, contentType string, body io.Reader, model string, options ...mediaHTTPOptions) (*http.Response, error) {
	key := ""
	if api != nil && api.APIKey != nil {
		key = strings.TrimSpace(*api.APIKey)
	}
	optionalKey := len(options) > 0 && options[0].optionalAPIKey
	if key == "" && !optionalKey {
		return nil, fmt.Errorf("media API key is required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("media endpoint suffix is missing")
	}
	var region *string
	if api != nil {
		region = api.Region
	}
	endpoint, err := resolveModelBaseURL(base, region)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(api), http.MethodPost, joinModelURL(endpoint, suffix), body)
	if err != nil {
		return nil, fmt.Errorf("create media request: %w", err)
	}
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	req.Header.Set("Content-Type", contentType)
	if len(options) > 0 && options[0].accept != "" {
		req.Header.Set("Accept", options[0].accept)
	}
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
func mediaJSONRequest(client *http.Client, base map[string]string, suffix string, api *APIConfig, body map[string]any, model string, options ...mediaHTTPOptions) (*http.Response, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("invalid media request JSON")
	}
	return mediaRequest(client, base, suffix, api, "application/json", bytes.NewReader(raw), model, options...)
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
func validateSpeechMIME(resp *http.Response, format string) error {
	typ, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || (!strings.HasPrefix(typ, "audio/") && typ != "application/octet-stream") {
		return fmt.Errorf("provider did not return audio")
	}
	expected := map[string]string{"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac", "audio/opus": "opus", "audio/aac": "aac"}[typ]
	if expected != "" && expected != format {
		return fmt.Errorf("provider audio MIME does not match requested format")
	}
	return nil
}

func readSpeechBytes(resp *http.Response, format string) (*TTSResponse, error) {
	if err := validateSpeechMIME(resp, format); err != nil {
		return nil, err
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

func streamSpeechBytes(resp *http.Response, format string, api *APIConfig, send func(*TTSResponse) error) error {
	if err := validateSpeechMIME(resp, format); err != nil {
		return err
	}
	buffer, total := make([]byte, 32*1024), 0
	for {
		n, err := resp.Body.Read(buffer)
		if n > 0 {
			if total+n > MaxMediaBytes {
				return fmt.Errorf("speech stream exceeds limit")
			}
			if err := requestContext(api).Err(); err != nil {
				return err
			}
			if err := send(&TTSResponse{Audio: bytes.Clone(buffer[:n]), Format: format}); err != nil {
				return err
			}
			total += n
		}
		if err != nil {
			if err != io.EOF {
				return err
			}
			if total == 0 {
				return fmt.Errorf("empty speech stream")
			}
			return requestContext(api).Err()
		}
	}
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
