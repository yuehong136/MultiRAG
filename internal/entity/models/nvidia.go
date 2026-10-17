package models

import "fmt"

// NVIDIAModel implements the initial NVIDIA chat contract; other capabilities stay unavailable.
type NVIDIAModel struct{ *openAIChatDriver }

func NewNVIDIAModel(baseURL map[string]string, suffix URLSuffix) *NVIDIAModel {
	driver := newOpenAIChatDriver(baseURL, suffix, "nvidia", nvidiaChatBody)
	driver.splitReasoning = true
	return &NVIDIAModel{driver}
}

func nvidiaChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config == nil || config.Temperature == nil {
		delete(body, "temperature")
	}
	if config != nil {
		if config.Thinking != nil {
			mode := "disabled"
			if *config.Thinking {
				mode = "enabled"
			}
			body["thinking"] = map[string]string{"type": mode}
		}
		if stream && config.DoSample != nil {
			body["do_sample"] = *config.DoSample
		}
	}
	return body, nil
}

func (n *NVIDIAModel) ListModels(api *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("nvidia: model discovery is not implemented")
}
func (n *NVIDIAModel) CheckConnection(api *APIConfig) error {
	return fmt.Errorf("nvidia: connection check is not implemented")
}

var _ ModelDriver = (*NVIDIAModel)(nil)
