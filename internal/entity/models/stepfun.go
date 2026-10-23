package models

// StepFunModel uses the shared OpenAI chat and discovery contracts.
type StepFunModel struct{ *openAIChatDriver }

func NewStepFunModel(baseURL map[string]string, suffix URLSuffix) *StepFunModel {
	return &StepFunModel{newOpenAIChatDriver(baseURL, suffix, "stepfun", stepFunChatBody)}
}

func stepFunChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if config == nil || config.Temperature == nil {
		delete(body, "temperature")
	}
	return body, err
}

var _ ModelDriver = (*StepFunModel)(nil)
