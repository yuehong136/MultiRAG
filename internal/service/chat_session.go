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
	"strings"
	"time"

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
}

// NewChatSessionService create chat session service
func NewChatSessionService() *ChatSessionService {
	return &ChatSessionService{
		chatSessionDAO:       dao.NewChatSessionDAO(),
		chatDAO:              dao.NewChatDAO(),
		userTenantDAO:        dao.NewUserTenantDAO(),
		modelProviderService: NewModelProviderService(),
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
			"name":        name,
			"user_id":     userID,
			"update_time": time.Now().UnixMilli(),
			"update_date": time.Now(),
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

	now := time.Now().Truncate(time.Second)
	createTime := time.Now().UnixMilli()

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
	session.CreateTime = &createTime
	session.CreateDate = &now
	session.UpdateTime = &createTime
	session.UpdateDate = &now

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
	// Get user's tenants
	tenantIDs, err := s.userTenantDAO.GetTenantIDsByUserID(userID)
	if err != nil {
		return nil, err
	}

	// Check if user is the owner of the dialog
	isOwner := false
	for _, tenantID := range tenantIDs {
		exists, err := s.chatSessionDAO.CheckDialogExists(tenantID, dialogID)
		if err != nil {
			return nil, err
		}
		if exists {
			isOwner = true
			break
		}
	}

	// Also check with userID as tenant
	if !isOwner {
		exists, err := s.chatSessionDAO.CheckDialogExists(userID, dialogID)
		if err != nil {
			return nil, err
		}
		isOwner = exists
	}

	if !isOwner {
		return nil, errors.New("Only owner of dialog authorized for this operation")
	}

	// List chat sessions
	sessions, err := s.chatSessionDAO.ListByDialogID(dialogID)
	if err != nil {
		return nil, err
	}

	return &ListChatSessionsResponse{Sessions: sessions}, nil
}

// prepareCompletion resolves stored session/dialog and validates the final user turn.
func (s *ChatSessionService) prepareCompletion(ctx context.Context, conversationID string, messages []map[string]interface{}, llmID string) (*entity.ChatSession, *entity.Chat, []interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, nil, nil, err
	}
	if len(messages) == 0 {
		return nil, nil, nil, errors.New("messages cannot be empty")
	}
	if messages[len(messages)-1]["role"] != "user" {
		return nil, nil, nil, errors.New("the last content of this conversation is not from user")
	}
	session, err := s.chatSessionDAO.GetByID(conversationID)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("Conversation not found: %w", err)
	}
	dialog, err := s.chatSessionDAO.GetDialogByID(session.DialogID)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("Dialog not found: %w", err)
	}
	if llmID != "" {
		dialog.LLMID = llmID
	}
	return session, dialog, s.initializeReference(session), nil
}

func (s *ChatSessionService) completionModel(ctx context.Context, dialog *entity.Chat) (*models.ChatModel, error) {
	model, err := s.modelProviderService.GetChatModel(dialog.TenantID, dialog.LLMID)
	if err != nil {
		return nil, err
	}
	model.APIConfig.Context = ctx
	return model, nil
}

// Completion uses a tenant-bound chat model with complete text or image history.
func (s *ChatSessionService) Completion(ctx context.Context, userID string, conversationID string, messages []map[string]interface{}, llmID string, config map[string]interface{}, messageID string) (map[string]interface{}, error) {
	session, dialog, reference, err := s.prepareCompletion(ctx, conversationID, messages, llmID)
	if err != nil {
		return nil, err
	}
	model, err := s.completionModel(ctx, dialog)
	if err != nil {
		return nil, err
	}
	history, err := s.modelMessages(s.processMessages(messages, dialog), s.buildSystemPrompt(dialog))
	if err != nil {
		return nil, err
	}
	response, err := model.ChatWithMessages(history, s.buildGenConf(dialog, config))
	if err != nil {
		return nil, err
	}
	if llmID == "" {
		if err := s.persistCompletion(ctx, session, messages, *response.Answer, messageID, reference); err != nil {
			return nil, err
		}
	}
	return map[string]interface{}{"answer": *response.Answer, "reasoning_content": response.ReasoningContent, "reference": reference[len(reference)-1], "final": true, "id": messageID, "session_id": session.ID}, nil
}

// CompletionStream runs the sender synchronously, so its error remains available
// to the HTTP handler and downstream cancellation never leaves a blocked channel.
func (s *ChatSessionService) CompletionStream(ctx context.Context, userID string, conversationID string, messages []map[string]interface{}, llmID string, config map[string]interface{}, messageID string, sender func(string) error) error {
	if sender == nil {
		return errors.New("completion sender is required")
	}
	session, dialog, reference, err := s.prepareCompletion(ctx, conversationID, messages, llmID)
	if err != nil {
		return err
	}
	model, err := s.completionModel(ctx, dialog)
	if err != nil {
		return err
	}
	var answer, reasoning strings.Builder
	sendData := func(data interface{}) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		bytes, err := json.Marshal(map[string]interface{}{"code": 0, "message": "", "data": data})
		if err != nil {
			return err
		}
		return sender("data: " + string(bytes) + "\n\n")
	}
	history, err := s.modelMessages(s.processMessages(messages, dialog), s.buildSystemPrompt(dialog))
	if err != nil {
		return err
	}
	err = model.ChatStreamlyWithMessages(history, s.buildGenConf(dialog, config), func(content, thought *string) error {
		if content != nil && *content == "[DONE]" {
			return nil
		}
		if thought != nil {
			reasoning.WriteString(*thought)
		}
		if content != nil {
			answer.WriteString(*content)
		}
		return sendData(map[string]interface{}{"answer": answer.String(), "reasoning_content": reasoning.String(), "reference": reference, "conversation_id": session.ID, "message_id": messageID})
	})
	if err != nil {
		return err
	}
	if llmID == "" {
		if err := s.persistCompletion(ctx, session, messages, answer.String(), messageID, reference); err != nil {
			return err
		}
	}
	return sendData(true)
}

func (s *ChatSessionService) persistCompletion(ctx context.Context, session *entity.ChatSession, messages []map[string]interface{}, answer, messageID string, reference []interface{}) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	stored := s.buildSessionMessages(session, messages)
	stored = append(stored, map[string]interface{}{"role": "assistant", "content": answer, "id": messageID})
	messageJSON, err := json.Marshal(map[string]interface{}{"messages": stored})
	if err != nil {
		return err
	}
	referenceJSON, err := json.Marshal(reference)
	if err != nil {
		return err
	}
	return dao.DB.WithContext(ctx).Model(&entity.ChatSession{}).Where("id = ?", session.ID).Updates(map[string]interface{}{"message": messageJSON, "reference": referenceJSON, "update_time": time.Now().UnixMilli(), "update_date": time.Now()}).Error
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

func (s *ChatSessionService) initializeReference(session *entity.ChatSession) []interface{} {
	var reference []interface{}
	if len(session.Reference) > 0 {
		json.Unmarshal(session.Reference, &reference)
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
	return filtered
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
