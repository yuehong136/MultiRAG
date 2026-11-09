package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

const DefaultAskTokenBudget = 4096

var ErrNoVisibleAnswer = errors.New("model returned no visible answer")

// AskRequest is distinct from retrieval_test's established kb_id contract.
type AskRequest struct {
	Question string   `json:"question"`
	KBIDs    []string `json:"kb_ids"`
	SearchID string   `json:"search_id,omitempty"`
}

// AnswerFrame is a cumulative visible answer with a separate reasoning channel.
// References always describe markers in Answer; the final frame is authoritative.
type AnswerFrame struct {
	Answer           string                 `json:"answer"`
	ReasoningContent string                 `json:"reasoning_content,omitempty"`
	Reference        map[string]interface{} `json:"reference"`
	Final            bool                   `json:"final"`
}

type answerRetrievalOptions struct {
	chatTenantID   string
	chatID         any
	rerankTenantID string
	search         *entity.Search
	config         map[string]interface{}
}

type AskService struct {
	chunks *ChunkService
	models *ModelProviderService
}

func NewAskService(chunks *ChunkService, provider *ModelProviderService) *AskService {
	return &AskService{chunks: chunks, models: provider}
}

func ValidateAskRequest(req *AskRequest) error {
	if req == nil || strings.TrimSpace(req.Question) == "" || len(req.Question) > 100000 || req.SearchID != strings.TrimSpace(req.SearchID) || len(req.SearchID) > 128 || len(req.KBIDs) == 0 || len(req.KBIDs) > 100 {
		return ErrRetrievalArgument
	}
	for _, id := range req.KBIDs {
		if !exactVectorID(id) || len(id) > 128 {
			return ErrRetrievalArgument
		}
	}
	return nil
}

// Stream authorizes every SQL resource before retrieval and binds the selected
// owner's chat/embedding models without falling back after lookup errors.
func (s *AskService) Stream(ctx context.Context, actor string, req *AskRequest, sender func(AnswerFrame) error) error {
	if ctx == nil || !exactVectorID(actor) {
		return ErrSearchbotAccessDenied
	}
	if err := ValidateAskRequest(req); err != nil {
		return err
	}
	if sender == nil || s == nil || s.chunks == nil || s.models == nil {
		return fmt.Errorf("ask service unavailable")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	owner := actor
	config := map[string]interface{}{}
	options := &answerRetrievalOptions{chatTenantID: owner, chatID: ""}
	page, size, top, threshold, weight := 1, 12, 1024, 0.1, 0.3
	retrieval := &RetrievalTestRequest{DatasetIDs: append([]string{}, req.KBIDs...), Question: req.Question, Page: &page, Size: &size, TopK: &top, SimilarityThreshold: &threshold, VectorSimilarityWeight: &weight, answer: options}
	if req.SearchID != "" {
		search, saved, err := readableRetrievalConfig(ctx, actor, req.SearchID)
		if err != nil {
			return err
		}
		owner, config = search.TenantID, saved
		options.chatTenantID, options.search, options.config = owner, search, saved
		if ids, exists := saved["kb_ids"]; exists {
			data, err := json.Marshal(ids)
			if err != nil || string(data) == "null" {
				return ErrRetrievalArgument
			}
			if err := json.Unmarshal(data, &retrieval.DatasetIDs); err != nil || len(retrieval.DatasetIDs) == 0 {
				return ErrRetrievalArgument
			}
		}
		retrieval.SearchID = &req.SearchID
	}
	if selector, exists := config["chat_id"]; exists && selector == nil {
		return ErrSearchbotArgument
	}
	selector, err := searchbotSelector(config["chat_id"])
	if err != nil {
		return err
	}
	options.chatID = selector
	settings, err := searchbotGeneration(entity.JSONMap(config))
	if err != nil {
		return err
	}
	// Summary generation never needs provider reasoning; inline think tags are
	// still parsed because providers may ignore this explicit configuration.
	settings["thinking"] = false
	enabled := true
	result, err := s.chunks.RetrievalTest(ctx, retrieval, actor)
	if err != nil {
		return err
	}
	if result == nil {
		return fmt.Errorf("retrieval returned no result")
	}
	chunks, err := NewSourcedChunks(result.Chunks)
	if err != nil {
		return err
	}
	prompt, err := LoadPrompt("ask_summary")
	if err != nil {
		return err
	}
	base := RenderPrompt(prompt, map[string]interface{}{"knowledge": ""})
	budget, err := answerKnowledgeBudget(DefaultAskTokenBudget, []models.Message{{Role: "system", Content: base}, {Role: "user", Content: req.Question}})
	if err != nil {
		return err
	}
	knowledge, selected, err := BuildKnowledgePrompt(chunks, budget)
	if err != nil {
		return err
	}
	if len(selected) == 0 {
		return sendAnswerFrame(ctx, sender, AnswerFrame{Answer: "Sorry, no relevant information provided.", Reference: answerReference(nil), Final: true})
	}
	model, err := NewSearchbotService(s.models).chatModel(ctx, owner, selector)
	if err != nil {
		return err
	}
	history := []models.Message{{Role: "system", Content: RenderPrompt(prompt, map[string]interface{}{"knowledge": knowledge})}, {Role: "user", Content: req.Question}}
	emit := func(frame AnswerFrame) error {
		return sender(withoutNoAnswerCitations(frame, "Sorry, no relevant information provided."))
	}
	answer, reasoning, err := streamVisibleAnswer(ctx, model, history, settings, selected, enabled, emit)
	if err != nil {
		return err
	}
	if isNoAnswer(answer, "Sorry, no relevant information provided.") {
		enabled = false
	}
	answer, refs, err := s.chunks.finalizeCitations(ctx, actor, answer, selected, result, enabled)
	if err != nil {
		return err
	}
	return sendAnswerFrame(ctx, sender, AnswerFrame{Answer: answer, ReasoningContent: reasoning, Reference: refs, Final: true})
}

func answerCitationSetting(config map[string]interface{}, key string, fallback bool) (bool, error) {
	value, exists := config[key]
	if !exists {
		return fallback, nil
	}
	enabled, ok := value.(bool)
	if !ok {
		return false, ErrRetrievalArgument
	}
	return enabled, nil
}

func answerKnowledgeBudget(total int, history []models.Message) (int, error) {
	data, err := json.Marshal(history)
	if err != nil {
		return 0, err
	}
	used, err := NumTokensFromString(string(data))
	if err != nil {
		return 0, err
	}
	return max(0, total-used), nil
}

func sendAnswerFrame(ctx context.Context, sender func(AnswerFrame) error, frame AnswerFrame) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := sender(frame); err != nil {
		return err
	}
	return ctx.Err()
}

func streamVisibleAnswer(ctx context.Context, model *models.ChatModel, history []models.Message, settings map[string]interface{}, chunks []SourcedChunk, citations bool, sender func(AnswerFrame) error) (string, string, error) {
	if model == nil || model.ModelDriver == nil || model.ModelName == nil {
		return "", "", fmt.Errorf("chat model unavailable")
	}
	local, api := *model, models.APIConfig{}
	if model.APIConfig != nil {
		api = *model.APIConfig
	}
	child, cancel := context.WithCancel(ctx)
	defer cancel()
	api.Context, local.APIConfig = child, &api
	var parser ThinkStreamState
	var answer, reasoning strings.Builder
	var firstError error
	err := local.ChatStreamlyWithSender(history, settings, func(content, thought *string) error {
		if firstError != nil {
			return firstError
		}
		if err := child.Err(); err != nil {
			firstError = err
			return err
		}
		if content != nil && *content == "[DONE]" {
			return nil
		}
		if thought != nil {
			reasoning.WriteString(*thought)
		}
		if content != nil {
			visible, private := parser.Feed(*content)
			answer.WriteString(visible)
			reasoning.WriteString(private)
		}
		visible, refs := cleanAnswerCitations(answer.String(), chunks, citations)
		firstError = sendAnswerFrame(child, sender, AnswerFrame{Answer: visible, ReasoningContent: reasoning.String(), Reference: answerReference(refs)})
		if firstError != nil {
			cancel()
		}
		return firstError
	})
	parser.Finish()
	if firstError != nil {
		return "", "", firstError
	}
	if err != nil {
		return "", "", err
	}
	if err := child.Err(); err != nil {
		return "", "", err
	}
	visible, _ := cleanAnswerCitations(answer.String(), chunks, citations)
	if strings.TrimSpace(visible) == "" {
		return "", "", ErrNoVisibleAnswer
	}
	return visible, reasoning.String(), nil
}
