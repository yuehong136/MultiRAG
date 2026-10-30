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

package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/google/uuid"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// ChatSessionService chat session (conversation) service
type ChatSessionService struct {
	chatSessionDAO       *dao.ChatSessionDAO
	chatDAO              *dao.ChatDAO
	userTenantDAO        *dao.UserTenantDAO
	modelProviderService *ModelProviderService
	chunks               *ChunkService
}

// NewChatSessionService create chat session service
func NewChatSessionService() *ChatSessionService {
	provider := NewModelProviderService()
	return NewChatSessionServiceWithModels(provider, NewChunkServiceWithModels(provider))
}

// NewChatSessionServiceWithModels uses one catalog for retrieval and generation.
func NewChatSessionServiceWithModels(provider *ModelProviderService, chunks *ChunkService) *ChatSessionService {
	return &ChatSessionService{
		chatSessionDAO:       dao.NewChatSessionDAO(),
		chatDAO:              dao.NewChatDAO(),
		userTenantDAO:        dao.NewUserTenantDAO(),
		modelProviderService: provider,
		chunks:               chunks,
	}
}

// SetChatSessionRequest set chat session request
type SetChatSessionRequest struct {
	SessionID string `json:"conversation_id,omitempty"`
	DialogID  string `json:"dialog_id,omitempty"`
	Name      string `json:"name,omitempty"`
	IsNew     bool   `json:"is_new"`
}

// SetChatSessionResponse set chat session response
type SetChatSessionResponse struct {
	*entity.ChatSession
}

// SetChatSession create or update a chat session
func (s *ChatSessionService) SetChatSession(userID string, req *SetChatSessionRequest) (*SetChatSessionResponse, error) {
	name := req.Name
	if name == "" {
		name = "New chat session"
	}
	// Limit name length to 255 characters
	if len(name) > 255 {
		name = name[:255]
	}

	if !req.IsNew {
		// Update existing chat session
		updates := map[string]interface{}{
			"name":    name,
			"user_id": userID,
		}

		if err := s.chatSessionDAO.UpdateByID(req.SessionID, updates); err != nil {
			return nil, errors.New("Chat session not found")
		}

		// Get updated chat session
		session, err := s.chatSessionDAO.GetByID(req.SessionID)
		if err != nil {
			return nil, errors.New("Fail to update a chat session")
		}

		return &SetChatSessionResponse{ChatSession: session}, nil
	}

	// Create new chat session
	// Check if dialog exists
	dialog, err := s.chatSessionDAO.GetDialogByID(req.DialogID)
	if err != nil {
		return nil, errors.New("Dialog not found")
	}

	// Generate UUID for new chat session
	newID := uuid.New().String()
	newID = strings.ReplaceAll(newID, "-", "")
	if len(newID) > 32 {
		newID = newID[:32]
	}

	// Get prologue from dialog's prompt_config
	prologue := "Hi! I'm your assistant. What can I do for you?"
	if dialog.PromptConfig != nil {
		if p, ok := dialog.PromptConfig["prologue"].(string); ok && p != "" {
			prologue = p
		}
	}

	// Create initial message - store as JSON object with messages array
	messagesObj := map[string]interface{}{
		"messages": []map[string]interface{}{
			{
				"role":    "assistant",
				"content": prologue,
			},
		},
	}
	messagesJSON, _ := json.Marshal(messagesObj)

	// Create reference - store as JSON array
	referenceJSON, _ := json.Marshal([]interface{}{})

	// Create chat session
	session := &entity.ChatSession{
		ID:        newID,
		DialogID:  req.DialogID,
		Name:      &name,
		Message:   messagesJSON,
		UserID:    &userID,
		Reference: referenceJSON,
	}

	if err := s.chatSessionDAO.Create(session); err != nil {
		return nil, errors.New("Fail to create a chat session")
	}

	return &SetChatSessionResponse{ChatSession: session}, nil
}

// RemoveChatSessionRequest remove chat sessions request
type RemoveChatSessionRequest struct {
	ChatSessions []string `json:"conversation_ids" binding:"required"`
}

// RemoveChatSessions removes chat sessions (hard delete)
func (s *ChatSessionService) RemoveChatSessions(userID string, chatSessions []string) error {
	// Get user's tenants
	tenantIDs, err := s.userTenantDAO.GetTenantIDsByUserID(userID)
	if err != nil {
		return err
	}

	// Build a set of user's tenant IDs for quick lookup
	tenantIDSet := make(map[string]bool)
	for _, tid := range tenantIDs {
		tenantIDSet[tid] = true
	}
	tenantIDSet[userID] = true

	// Check each chat session
	for _, convID := range chatSessions {
		// Get the chat session
		session, err := s.chatSessionDAO.GetByID(convID)
		if err != nil {
			return fmt.Errorf("Chat session not found: %s", convID)
		}

		// Check if user is the owner by checking dialog ownership
		isOwner := false
		for tenantID := range tenantIDSet {
			exists, err := s.chatSessionDAO.CheckDialogExists(tenantID, session.DialogID)
			if err != nil {
				return err
			}
			if exists {
				isOwner = true
				break
			}
		}

		if !isOwner {
			return errors.New("Only owner of chat session authorized for this operation")
		}

		// Delete the chat session
		if err := s.chatSessionDAO.DeleteByID(convID); err != nil {
			return err
		}
	}

	return nil
}

// ListChatSessionsRequest list chat sessions request
type ListChatSessionsRequest struct {
	DialogID string `json:"dialog_id" binding:"required"`
}

// ListChatSessionsResponse list chat sessions response
type ListChatSessionsResponse struct {
	Sessions []*entity.ChatSession
}

// ListChatSessions lists chat sessions for a dialog
func (s *ChatSessionService) ListChatSessions(userID string, dialogID string) (*ListChatSessionsResponse, error) {
	// Match Python REST's tenant-owned chat contract. Membership does not
	// grant access to another tenant's private conversations.
	exists, err := s.chatSessionDAO.CheckDialogExists(userID, dialogID)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, errors.New("Only owner of dialog authorized for this operation")
	}

	// List chat sessions
	sessions, err := s.chatSessionDAO.ListByDialogID(dialogID)
	if err != nil {
		return nil, err
	}

	return &ListChatSessionsResponse{Sessions: sessions}, nil
}

// prepareCompletion binds the actual actor to both the conversation and dialog
// before any model resolution, retrieval, or write. No tenant fallback is used.
func (s *ChatSessionService) prepareCompletion(ctx context.Context, actor, conversationID string, messages []map[string]interface{}, llmID string) (*entity.ChatSession, *entity.Chat, []interface{}, error) {
	if ctx == nil || !exactVectorID(actor) || !exactVectorID(conversationID) {
		return nil, nil, nil, ErrChatSessionAccessDenied
	}
	if err := ctx.Err(); err != nil {
		return nil, nil, nil, err
	}
	if len(messages) == 0 || messages[len(messages)-1]["role"] != "user" {
		return nil, nil, nil, ErrRetrievalArgument
	}
	if dao.DB == nil {
		return nil, nil, nil, errors.New("chat database unavailable")
	}
	var session entity.ChatSession
	if err := authorizedChatSessionQuery(ctx, actor).Where("id = ?", conversationID).First(&session).Error; err != nil {
		return nil, nil, nil, chatSessionReadError(err)
	}
	var dialog entity.Chat
	if err := dao.DB.WithContext(ctx).Where("id = ? AND status = ?", session.DialogID, "1").First(&dialog).Error; err != nil {
		return nil, nil, nil, chatSessionReadError(err)
	}
	if err := loadExactDialogConfig(ctx, &dialog); err != nil {
		return nil, nil, nil, err
	}
	if llmID != "" {
		selector, err := searchbotSelector(llmID)
		if err != nil {
			return nil, nil, nil, err
		}
		if id, err := strconv.ParseInt(selector, 10, 64); err == nil {
			dialog.TenantLLMID = &id
		} else {
			dialog.LLMID = selector
			dialog.TenantLLMID = nil
		}
	}
	reference, err := s.initializeReference(&session)
	return &session, &dialog, reference, err
}

func (s *ChatSessionService) completionModel(ctx context.Context, dialog *entity.Chat) (*models.ChatModel, error) {
	var model *models.ChatModel
	var err error
	if dialog.TenantLLMID != nil {
		model, err = s.modelProviderService.GetChatModelByIDWithContext(ctx, dialog.TenantID, *dialog.TenantLLMID)
	} else {
		model, err = s.modelProviderService.GetChatModelWithContext(ctx, dialog.TenantID, dialog.LLMID)
	}
	if err != nil {
		return nil, err
	}
	if model == nil || model.ModelDriver == nil || model.ModelName == nil {
		return nil, errors.New("chat model unavailable")
	}
	local, api := *model, models.APIConfig{}
	if model.APIConfig != nil {
		api = *model.APIConfig
	}
	api.Context, local.APIConfig = ctx, &api
	return &local, nil
}

// Completion uses authorized retrieval, complete history and the same visible
// answer/reference projection as streaming before committing a successful turn.
func (s *ChatSessionService) Completion(ctx context.Context, userID string, conversationID string, messages []map[string]interface{}, llmID string, config map[string]interface{}, messageID string) (map[string]interface{}, error) {
	session, dialog, reference, err := s.prepareCompletion(ctx, userID, conversationID, messages, llmID)
	if err != nil {
		return nil, err
	}
	messages, err = completionMessages(session, messages)
	if err != nil {
		return nil, err
	}
	knowledge, err := s.completionKnowledge(ctx, userID, dialog, messages)
	if err != nil {
		return nil, err
	}
	var answer, reasoning string
	if knowledge.emptyAnswer != nil {
		answer = *knowledge.emptyAnswer
	} else {
		model, err := s.completionModel(ctx, dialog)
		if err != nil {
			return nil, err
		}
		response, err := model.ChatWithMessages(knowledge.history, s.buildGenConf(dialog, config))
		if err != nil {
			return nil, err
		}
		var parser ThinkStreamState
		answer, reasoning = parser.Feed(*response.Answer)
		parser.Finish()
		if response.ReasoningContent != nil {
			reasoning = *response.ReasoningContent + reasoning
		}
		if strings.TrimSpace(answer) == "" {
			return nil, ErrNoVisibleAnswer
		}
	}
	answer, refs, err := s.finalizeCompletion(ctx, userID, knowledge, answer)
	if err != nil {
		return nil, err
	}
	reference[len(reference)-1] = refs
	if llmID == "" {
		if err := s.persistCompletion(ctx, userID, session, messages, answer, messageID, reference); err != nil {
			return nil, err
		}
	}
	return map[string]interface{}{"answer": answer, "reasoning_content": reasoning, "reference": refs, "final": true, "id": messageID, "session_id": session.ID}, nil
}

// CompletionStream keeps all provider and sender calls synchronous. A failed
// data send or cancellation before persistence leaves SQL unchanged. Once SQL
// commits, delivery of the terminal frame cannot be atomic with that commit.
func (s *ChatSessionService) CompletionStream(ctx context.Context, userID string, conversationID string, messages []map[string]interface{}, llmID string, config map[string]interface{}, messageID string, sender func(string) error) error {
	if sender == nil {
		return errors.New("completion sender is required")
	}
	session, dialog, reference, err := s.prepareCompletion(ctx, userID, conversationID, messages, llmID)
	if err != nil {
		return err
	}
	messages, err = completionMessages(session, messages)
	if err != nil {
		return err
	}
	knowledge, err := s.completionKnowledge(ctx, userID, dialog, messages)
	if err != nil {
		return err
	}
	sendData := func(data interface{}) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		raw, err := json.Marshal(map[string]interface{}{"code": 0, "message": "", "data": data})
		if err != nil {
			return err
		}
		if err := sender("data: " + string(raw) + "\n\n"); err != nil {
			return err
		}
		return ctx.Err()
	}
	sendFrame := func(frame AnswerFrame) error {
		frame = withoutNoAnswerCitations(frame, knowledge.noAnswer)
		return sendData(map[string]interface{}{"answer": frame.Answer, "reasoning_content": frame.ReasoningContent, "reference": frame.Reference, "final": frame.Final, "conversation_id": session.ID, "message_id": messageID})
	}
	var answer, reasoning string
	if knowledge.emptyAnswer != nil {
		answer = *knowledge.emptyAnswer
	} else {
		model, err := s.completionModel(ctx, dialog)
		if err != nil {
			return err
		}
		answer, reasoning, err = streamVisibleAnswer(ctx, model, knowledge.history, s.buildGenConf(dialog, config), knowledge.chunks, knowledge.citations, sendFrame)
		if err != nil {
			return err
		}
	}
	answer, refs, err := s.finalizeCompletion(ctx, userID, knowledge, answer)
	if err != nil {
		return err
	}
	// Give the sender the decorated content before the SQL commit; an ordinary
	// send failure here must not persist a successful assistant message.
	frame := AnswerFrame{Answer: answer, ReasoningContent: reasoning, Reference: refs}
	if err := sendFrame(frame); err != nil {
		return err
	}
	reference[len(reference)-1] = refs
	if llmID == "" {
		if err := s.persistCompletion(ctx, userID, session, messages, answer, messageID, reference); err != nil {
			return err
		}
	}
	frame.Final = true
	if err := sendFrame(frame); err != nil {
		return err
	}
	return sendData(true)
}

func (s *ChatSessionService) persistCompletion(ctx context.Context, actor string, session *entity.ChatSession, messages []map[string]interface{}, answer, messageID string, reference []interface{}) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	stored := s.buildSessionMessages(session, messages)
	stored = append(stored, map[string]interface{}{"role": "assistant", "content": answer, "id": messageID})
	raw, err := json.Marshal(map[string]interface{}{"messages": stored})
	if err != nil {
		return err
	}
	refs, err := json.Marshal(reference)
	if err != nil {
		return err
	}
	// Recheck scope at the write and use the original snapshot to reject concurrent
	// completions instead of silently losing an already committed conversation turn.
	query := authorizedChatSessionQuery(ctx, actor).Where("id = ? AND dialog_id = ?", session.ID, session.DialogID)
	if len(session.Message) > 0 {
		query = query.Where("message = ?", session.Message)
	} else {
		query = query.Where("message IS NULL")
	}
	result := query.Updates(map[string]interface{}{"message": raw, "reference": refs})
	if result.Error != nil {
		return result.Error
	}
	if result.RowsAffected != 1 {
		return errors.New("conversation changed or access was revoked")
	}
	return nil
}

// Helper methods

func (s *ChatSessionService) buildSessionMessages(session *entity.ChatSession, messages []map[string]interface{}) []map[string]interface{} {
	// Deep copy messages to session
	sessionMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		sessionMessages[i] = make(map[string]interface{})
		for k, v := range msg {
			sessionMessages[i][k] = v
		}
	}
	return sessionMessages
}

func (s *ChatSessionService) initializeReference(session *entity.ChatSession) ([]interface{}, error) {
	var reference []interface{}
	if len(session.Reference) > 0 {
		if err := json.Unmarshal(session.Reference, &reference); err != nil {
			return nil, errors.New("invalid stored references")
		}
	}
	// Filter out nil entries and append new reference
	var filtered []interface{}
	for _, r := range reference {
		if r != nil {
			filtered = append(filtered, r)
		}
	}
	filtered = append(filtered, map[string]interface{}{
		"chunks":   []interface{}{},
		"doc_aggs": []interface{}{},
	})
	return filtered, nil
}

// buildSystemPrompt builds the system prompt from dialog configuration
func (s *ChatSessionService) buildSystemPrompt(dialog *entity.Chat) string {
	if dialog.PromptConfig == nil {
		return ""
	}

	system, _ := dialog.PromptConfig["system"].(string)
	return system
}

// processMessages processes messages and handles attachments
func (s *ChatSessionService) processMessages(messages []map[string]interface{}, dialog *entity.Chat) []map[string]interface{} {
	// Process each message
	processed := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		processed[i] = make(map[string]interface{})
		for k, v := range msg {
			processed[i][k] = v
		}

		// Clean content - remove file markers
		if content, ok := msg["content"].(string); ok {
			content = s.cleanContent(content)
			processed[i]["content"] = content
		}
	}

	return processed
}

// cleanContent removes file markers from content
func (s *ChatSessionService) cleanContent(content string) string {
	// Remove ##N$$ markers
	// This is a simplified version - full implementation would use regex
	return content
}

// modelMessages retains every conversational content value for validation by the model.
// The configured system prompt remains authoritative, as in the legacy session path.
func (s *ChatSessionService) modelMessages(messages []map[string]interface{}, system string) ([]models.Message, error) {
	history := []models.Message{}
	if system != "" {
		history = append(history, models.Message{Role: "system", Content: system})
	}
	for _, msg := range messages {
		role, ok := msg["role"].(string)
		if !ok || role == "" {
			return nil, errors.New("message role is required")
		}
		if role == "system" {
			continue
		}
		item := models.Message{Role: role, Content: msg["content"]}
		if value, present := msg["reasoning_content"]; present && value != nil {
			text, ok := value.(string)
			if !ok {
				return nil, errors.New("invalid reasoning_content")
			}
			item.ReasoningContent = &text
		}
		history = append(history, item)
	}
	if err := models.ValidateMessages(history); err != nil {
		return nil, err
	}
	return history, nil
}

// buildGenConf builds generation config from dialog and request
func (s *ChatSessionService) buildGenConf(dialog *entity.Chat, config map[string]interface{}) map[string]interface{} {
	genConf := make(map[string]interface{})

	// Start with dialog's LLM setting
	if dialog.LLMSetting != nil {
		for k, v := range dialog.LLMSetting {
			genConf[k] = v
		}
	}

	// Override with request config
	for k, v := range config {
		genConf[k] = v
	}

	return genConf
}
