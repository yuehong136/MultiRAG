package models

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"strings"
)

// The legacy provider payload builders keep their own thinking options, while
// their responses share one bounded, typed OpenAI text contract.
func decodeLegacyChatResponse(reader io.Reader) (*ChatResponse, error) {
	var response struct {
		Choices []*struct {
			FinishReason *string `json:"finish_reason"`
			Message      *struct {
				Content   *string `json:"content"`
				Reasoning *string `json:"reasoning_content"`
			} `json:"message"`
		} `json:"choices"`
		Error json.RawMessage `json:"error"`
		Code  json.RawMessage `json:"code"`
	}
	if err := decodeBoundedModelJSON(reader, &response); err != nil {
		return nil, fmt.Errorf("invalid model chat response")
	}
	if modelResponseHasError(response.Error) {
		return nil, fmt.Errorf("provider returned a chat error")
	}
	if err := validateModelResponseCode(response.Code); err != nil {
		return nil, err
	}
	if len(response.Choices) == 0 {
		return nil, fmt.Errorf("model chat response has no choices")
	}
	for _, choice := range response.Choices {
		if choice == nil || choice.Message == nil || choice.Message.Content == nil || strings.TrimSpace(*choice.Message.Content) == "" {
			return nil, fmt.Errorf("model chat response has no text answer")
		}
		if err := validateStrictChatFinish(choice.FinishReason); err != nil {
			return nil, err
		}
	}
	message := response.Choices[0].Message
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.Reasoning}, nil
}

func modelResponseHasError(raw json.RawMessage) bool {
	return len(raw) != 0 && string(raw) != "null"
}

// readLegacyChatStream requires complete SSE frames and validates every event
// before delivering its content. A provider terminal, a visible text answer and
// successful sender calls are all required before forwarding [DONE].
func readLegacyChatStream(reader io.Reader, api *APIConfig, requireDone bool, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("model stream sender is required")
	}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer, finished := false, false
	complete := func() error {
		if err := requestContext(api).Err(); err != nil {
			return err
		}
		if !receivedAnswer {
			return fmt.Errorf("model stream returned no text answer")
		}
		done := "[DONE]"
		return sender(&done, nil)
	}
	for {
		if err := requestContext(api).Err(); err != nil {
			return err
		}
		data, ok, err := nextOpenAIStreamData(scanner, true)
		if ctxErr := requestContext(api).Err(); ctxErr != nil {
			return ctxErr
		}
		if err != nil {
			return err
		}
		if !ok {
			if finished && !requireDone {
				return complete()
			}
			return io.ErrUnexpectedEOF
		}
		if strings.TrimSpace(data) == "" {
			continue
		}
		if data == "[DONE]" {
			return complete()
		}
		var event struct {
			Choices *[]*openAIStreamChoice `json:"choices"`
			Error   json.RawMessage        `json:"error"`
			Usage   json.RawMessage        `json:"usage"`
			Code    json.RawMessage        `json:"code"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("invalid model stream event")
		}
		if modelResponseHasError(event.Error) {
			return fmt.Errorf("provider returned a stream error")
		}
		if err := validateModelResponseCode(event.Code); err != nil {
			return err
		}
		if event.Choices == nil {
			return fmt.Errorf("model stream event is missing choices")
		}
		if len(*event.Choices) == 0 {
			// OpenAI-compatible providers may emit a final usage-only event.
			var usage map[string]json.RawMessage
			if len(event.Usage) == 0 || json.Unmarshal(event.Usage, &usage) != nil || usage == nil {
				return fmt.Errorf("model stream event has no choices")
			}
			continue
		}
		for _, choice := range *event.Choices {
			if choice == nil {
				return fmt.Errorf("invalid model stream choice")
			}
			if err := validateStrictChatFinish(choice.FinishReason); err != nil {
				return err
			}
			if choice.Delta == nil && (choice.FinishReason == nil || *choice.FinishReason == "") {
				return fmt.Errorf("invalid model stream delta")
			}
			if choice.Delta != nil {
				var content string
				if raw := choice.Delta.Content; len(raw) > 0 && string(raw) != "null" {
					if json.Unmarshal(raw, &content) != nil {
						return fmt.Errorf("invalid model stream content")
					}
				}
				if finished && (content != "" || choice.Delta.Reasoning != nil && *choice.Delta.Reasoning != "") {
					return fmt.Errorf("model stream continued after completion")
				}
			}
		}
		choice := (*event.Choices)[0]
		if choice.Delta != nil {
			delta := choice.Delta
			if delta.Reasoning != nil && *delta.Reasoning != "" {
				if err := sender(nil, delta.Reasoning); err != nil {
					return err
				}
			}
			if err := requestContext(api).Err(); err != nil {
				return err
			}
			var content string
			if len(delta.Content) > 0 && string(delta.Content) != "null" {
				if json.Unmarshal(delta.Content, &content) != nil {
					return fmt.Errorf("invalid model stream content")
				}
			}
			if content != "" {
				if err := sender(&content, nil); err != nil {
					return err
				}
				receivedAnswer = receivedAnswer || strings.TrimSpace(content) != ""
			}
		}
		finished = finished || choice.FinishReason != nil && *choice.FinishReason != ""
	}
}
