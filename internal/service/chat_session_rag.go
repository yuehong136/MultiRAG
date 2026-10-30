package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"gorm.io/gorm"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

var ErrChatSessionAccessDenied = errors.New("conversation is not accessible")

// A conversation belongs to its actual user or the dialog owner. Active tenant
// membership alone never permits another member's private conversation.
func authorizedChatSessionQuery(ctx context.Context, actor string) *gorm.DB {
	return dao.DB.WithContext(ctx).Model(&entity.ChatSession{}).
		Where("EXISTS (SELECT 1 FROM t_ai_dialogs AS dialog WHERE dialog.id = t_ai_conversations.dialog_id AND dialog.status = ? AND (dialog.tenant_id = ? OR t_ai_conversations.user_id = ?) AND EXISTS (SELECT 1 FROM t_ai_user_tenants AS membership WHERE membership.tenant_id = dialog.tenant_id AND membership.user_id = ? AND membership.status = ? AND membership.role IN ('owner', 'admin', 'normal')) AND EXISTS (SELECT 1 FROM t_ai_tenants AS owner_tenant WHERE owner_tenant.id = dialog.tenant_id AND owner_tenant.status = ?) AND EXISTS (SELECT 1 FROM t_ai_users AS owner_user WHERE owner_user.id = dialog.tenant_id AND owner_user.status = ?))", "1", actor, actor, actor, "1", "1", "1").
		Where("EXISTS (SELECT 1 FROM t_ai_users AS actor WHERE actor.id = ? AND actor.status = ?)", actor, "1")
}

func chatSessionReadError(err error) error {
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return ErrChatSessionAccessDenied
	}
	return err
}

// JSONMap's compatibility scanner uses float64. Completion re-reads its own
// authorized dialog JSON with UseNumber before interpreting filter operands.
func loadExactDialogConfig(ctx context.Context, dialog *entity.Chat) error {
	var prompt, setting, filter []byte
	if err := dao.DB.WithContext(ctx).Model(&entity.Chat{}).Select("prompt_config", "llm_setting", "meta_data_filter").Where("id = ? AND tenant_id = ? AND status = ?", dialog.ID, dialog.TenantID, "1").Row().Scan(&prompt, &setting, &filter); err != nil {
		return err
	}
	for _, item := range []struct {
		raw []byte
		dst *entity.JSONMap
	}{{prompt, &dialog.PromptConfig}, {setting, &dialog.LLMSetting}} {
		if len(item.raw) == 0 || string(item.raw) == "null" {
			*item.dst = nil
			continue
		}
		decoder := json.NewDecoder(bytes.NewReader(item.raw))
		decoder.UseNumber()
		if err := decoder.Decode(item.dst); err != nil {
			return ErrRetrievalArgument
		}
	}
	if len(filter) == 0 || string(filter) == "null" {
		dialog.MetaDataFilter = nil
	} else {
		var parsed entity.JSONMap
		decoder := json.NewDecoder(bytes.NewReader(filter))
		decoder.UseNumber()
		if err := decoder.Decode(&parsed); err != nil {
			return ErrRetrievalArgument
		}
		dialog.MetaDataFilter = &parsed
	}
	return nil
}

func completionMessages(session *entity.ChatSession, incoming []map[string]interface{}) ([]map[string]interface{}, error) {
	if len(incoming) != 1 || len(session.Message) == 0 {
		return incoming, nil
	}
	var stored struct {
		Messages []map[string]interface{} `json:"messages"`
	}
	decoder := json.NewDecoder(bytes.NewReader(session.Message))
	decoder.UseNumber()
	if err := decoder.Decode(&stored); err != nil {
		return nil, errors.New("invalid stored conversation messages")
	}
	if id, ok := incoming[0]["id"].(string); ok && id != "" {
		for i, message := range stored.Messages {
			if message["role"] == "user" && message["id"] == id {
				stored.Messages = stored.Messages[:i]
				break
			}
		}
	}
	return append(stored.Messages, incoming...), nil
}

type completionKnowledge struct {
	history     []models.Message
	chunks      []SourcedChunk
	retrieval   *RetrievalTestResponse
	citations   bool
	emptyAnswer *string
	noAnswer    string
}

func (s *ChatSessionService) completionKnowledge(ctx context.Context, actor string, dialog *entity.Chat, messages []map[string]interface{}) (*completionKnowledge, error) {
	system := s.buildSystemPrompt(dialog)
	history, err := s.modelMessages(s.processMessages(messages, dialog), system)
	if err != nil {
		return nil, err
	}
	knowledge := &completionKnowledge{history: history}
	if len(dialog.KBIDs) == 0 {
		return knowledge, nil
	}
	if s.chunks == nil {
		return nil, errors.New("chat retrieval unavailable")
	}
	kbIDs := []string{}
	for _, value := range dialog.KBIDs {
		id, ok := value.(string)
		if !ok || !exactVectorID(id) {
			return nil, ErrRetrievalArgument
		}
		kbIDs = append(kbIDs, id)
	}
	question := completionQuestion(messages[len(messages)-1]["content"])
	if question == "" {
		return nil, fmt.Errorf("%w: a text question is required for retrieval", ErrRetrievalArgument)
	}
	if dialog.TopN < 0 || dialog.TopN > 100 || dialog.TopK < 0 || dialog.TopK > 2048 {
		return nil, ErrRetrievalArgument
	}
	docIDs, err := completionDocIDs(messages[len(messages)-1])
	if err != nil {
		return nil, err
	}
	page, size, top := 1, max(1, int(dialog.TopN)), int(dialog.TopK)
	if dialog.TopN == 0 {
		docIDs = []string{}
	}
	req := &RetrievalTestRequest{DatasetIDs: kbIDs, Question: question, Page: &page, Size: &size, DocIDs: docIDs, TopK: &top, SimilarityThreshold: &dialog.SimilarityThreshold, VectorSimilarityWeight: &dialog.VectorSimilarityWeight, TenantRerankID: dialog.TenantRerankID, answer: &answerRetrievalOptions{chatTenantID: dialog.TenantID, chatID: dialog.LLMID, rerankTenantID: dialog.TenantID}}
	if dialog.TenantLLMID != nil {
		req.answer.chatID = *dialog.TenantLLMID
	}
	if dialog.TenantRerankID == nil && dialog.RerankID != "" {
		req.RerankID = &dialog.RerankID
	}
	if dialog.MetaDataFilter != nil {
		req.Filter = map[string]interface{}(*dialog.MetaDataFilter)
	}
	citations, err := answerCitationSetting(dialog.PromptConfig, "quote", true)
	if err != nil {
		return nil, err
	}
	citations = citations && dialog.DoRefer != "0"
	if value, exists := dialog.PromptConfig["empty_response"]; exists {
		text, ok := value.(string)
		if !ok {
			return nil, ErrRetrievalArgument
		}
		knowledge.noAnswer = text
	}
	result, err := s.chunks.RetrievalTest(ctx, req, actor)
	if err != nil {
		return nil, err
	}
	if result == nil {
		return nil, errors.New("chat retrieval returned no result")
	}
	chunks, err := NewSourcedChunks(result.Chunks)
	if err != nil {
		return nil, err
	}
	budget, err := answerKnowledgeBudget(DefaultAskTokenBudget, history)
	if err != nil {
		return nil, err
	}
	prompt, selected, err := BuildKnowledgePrompt(chunks, budget)
	if err != nil {
		return nil, err
	}
	knowledge.chunks, knowledge.retrieval, knowledge.citations = selected, result, citations
	if len(selected) == 0 {
		if value, exists := dialog.PromptConfig["empty_response"]; exists {
			text, ok := value.(string)
			if !ok {
				return nil, ErrRetrievalArgument
			}
			if text != "" {
				knowledge.emptyAnswer = &text
				return knowledge, nil
			}
		}
	}
	// Replace only configured placeholders; retain all original turns and image
	// blocks. Without a placeholder, append knowledge to the configured system.
	if strings.Contains(system, "{knowledge}") {
		system = strings.ReplaceAll(system, "{knowledge}", prompt)
	} else {
		system += "\n\nUse the following knowledge to answer the question. If it is insufficient, say so.\n" + prompt
	}
	knowledge.history, err = s.modelMessages(s.processMessages(messages, dialog), system)
	return knowledge, err
}

func completionQuestion(content any) string {
	if text, ok := content.(string); ok {
		return strings.TrimSpace(text)
	}
	parts := []string{}
	add := func(block map[string]interface{}) {
		if block["type"] == "text" {
			if text, ok := block["text"].(string); ok {
				parts = append(parts, text)
			}
		}
	}
	switch blocks := content.(type) {
	case []interface{}:
		for _, value := range blocks {
			if block, ok := value.(map[string]interface{}); ok {
				add(block)
			}
		}
	case []map[string]interface{}:
		for _, block := range blocks {
			add(block)
		}
	}
	return strings.TrimSpace(strings.Join(parts, "\n"))
}

func completionDocIDs(message map[string]interface{}) ([]string, error) {
	value, exists := message["doc_ids"]
	if !exists || value == nil {
		return nil, nil
	}
	data, err := json.Marshal(value)
	if err != nil {
		return nil, ErrRetrievalArgument
	}
	var ids []string
	if err := json.Unmarshal(data, &ids); err != nil {
		return nil, ErrRetrievalArgument
	}
	if ids == nil {
		return nil, nil
	}
	for _, id := range ids {
		if !exactVectorID(id) {
			return nil, ErrRetrievalArgument
		}
	}
	return vectorIDs(ids)
}

func (s *ChatSessionService) finalizeCompletion(ctx context.Context, actor string, knowledge *completionKnowledge, answer string) (string, map[string]interface{}, error) {
	if len(knowledge.chunks) == 0 {
		answer, _ = cleanAnswerCitations(answer, nil, false)
		return answer, answerReference(nil), ctx.Err()
	}
	citations := knowledge.citations && !isNoAnswer(answer, knowledge.noAnswer)
	return s.chunks.finalizeCitations(ctx, actor, answer, knowledge.chunks, knowledge.retrieval, citations)
}

// A configured no-answer line describes missing evidence. Its text remains
// visible, but neither model-authored markers nor embedding matches are sources.
func isNoAnswer(answer, noAnswer string) bool {
	clean, _ := cleanAnswerCitations(answer, nil, false)
	return strings.TrimSpace(noAnswer) != "" && strings.TrimSpace(clean) == strings.TrimSpace(noAnswer)
}
func withoutNoAnswerCitations(frame AnswerFrame, noAnswer string) AnswerFrame {
	if isNoAnswer(frame.Answer, noAnswer) {
		frame.Answer, _ = cleanAnswerCitations(frame.Answer, nil, false)
		frame.Reference = answerReference(nil)
	}
	return frame
}
