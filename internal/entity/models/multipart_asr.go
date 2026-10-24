package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"mime/multipart"
	"net/http"
	"sort"
	"strings"
)

// multipartASR writes all model/options fields before the audio file, for providers
// that begin processing the file part before reading trailing form fields.
func multipartASR(client *http.Client, base map[string]string, suffix string, name *string, audio []byte, api *APIConfig, config *ASRConfig, options ...mediaHTTPOptions) (*ASRResponse, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("ASR model required")
	}
	if err := ValidateMediaBytes(audio); err != nil {
		return nil, err
	}
	if err := ValidateASRConfig(config); err != nil {
		return nil, err
	}
	var params map[string]any
	format := "wav"
	if config != nil {
		params = config.Params
		if config.Format != "" {
			format = config.Format
		}
	}
	fields, err := CloneMediaParams(params)
	if err != nil {
		return nil, err
	}
	if config != nil {
		if len(config.Language) == 1 {
			fields["language"] = config.Language[0]
		} else if len(config.Language) > 1 {
			fields["language"] = config.Language
		}
		if config.Prompt != "" {
			fields["prompt"] = config.Prompt
		}
	}
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("model", *name); err != nil {
		return nil, err
	}
	keys := make([]string, 0, len(fields))
	for key := range fields {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		value, ok := fields[key].(string)
		if !ok {
			raw, err := json.Marshal(fields[key])
			if err != nil {
				return nil, fmt.Errorf("invalid ASR option")
			}
			value = string(raw)
		}
		if err := writer.WriteField(key, value); err != nil {
			return nil, err
		}
	}
	part, err := writer.CreateFormFile("file", "audio."+format)
	if err != nil {
		return nil, err
	}
	if _, err := part.Write(audio); err != nil {
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaRequest(client, base, suffix, local, writer.FormDataContentType(), &body, "", options...)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := readMediaBody(resp.Body, MaxMediaBytes+MaxMediaParamsBytes)
	if err != nil {
		return nil, err
	}
	var result struct {
		Text  *string         `json:"text"`
		Error json.RawMessage `json:"error"`
		Code  *int            `json:"code"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, fmt.Errorf("invalid transcription response")
	}
	if len(result.Error) > 0 && string(result.Error) != "null" || result.Code != nil && *result.Code != 0 {
		return nil, fmt.Errorf("transcription provider error")
	}
	response := &ASRResponse{Text: result.Text}
	if err := ValidateASRResponse(response); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return response, nil
}
