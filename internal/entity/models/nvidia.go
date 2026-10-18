package models

// NVIDIAModel implements NVIDIA chat and instance model discovery.
type NVIDIAModel struct{ *openAIChatDriver }

func NewNVIDIAModel(baseURL map[string]string, suffix URLSuffix) *NVIDIAModel {
	driver := newOpenAIChatDriver(baseURL, suffix, "nvidia", nvidiaChatBody)
	driver.splitReasoning = true
	driver.optionalDiscoveryKey = true
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

var _ ModelDriver = (*NVIDIAModel)(nil)
