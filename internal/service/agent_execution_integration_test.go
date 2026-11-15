package service

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

func targetAgentDB(t *testing.T) (*AgentService, *gorm.DB) {
	t.Helper()
	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{TranslateError: true})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.AutoMigrate(&entity.UserCanvas{}, &entity.UserCanvasVersion{}, &entity.API4Conversation{}, &entity.UserTenant{}); err != nil {
		t.Fatal(err)
	}
	rawDB, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	rawDB.SetMaxOpenConns(1)
	old := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = old; raw, _ := db.DB(); _ = raw.Close() })
	return NewAgentService(), db
}

func TestAgentExecutionIntegrationCRUDAndAuthorization(t *testing.T) {
	s, db := targetAgentDB(t)
	title := "target"
	row, code, err := s.CreateAgent(t.Context(), &CreateAgentRequest{UserID: "owner", Title: &title, DSL: entity.JSONMap{"exact": json.Number("9007199254740993")}})
	if err != nil || code != common.CodeSuccess || row.CanvasCategory != "agent_canvas" {
		t.Fatalf("create: %v %v", code, err)
	}
	if _, code, err = s.CreateAgent(t.Context(), &CreateAgentRequest{UserID: "owner", Title: &title, DSL: entity.JSONMap{}}); err == nil || code == common.CodeSuccess {
		t.Fatal("default-category duplicate accepted")
	}
	if _, code, err = s.CreateAgent(t.Context(), &CreateAgentRequest{UserID: "owner", Title: &title, CanvasCategory: "pipeline", DSL: entity.JSONMap{}}); err != nil || code != 0 {
		t.Fatal("category ignored", code, err)
	}
	if _, err := s.GetAgent(t.Context(), "foreign", row.ID); !errors.Is(err, dao.ErrUserCanvasNotFound) {
		t.Fatal("foreign read", err)
	}
	if err := s.UpdateAgent(t.Context(), "foreign", row.ID, entity.JSONMap{"changed": true}); !errors.Is(err, dao.ErrUserCanvasNotFound) {
		t.Fatal("foreign update", err)
	}
	updatedTitle, description := "released title", "released description"
	version, err := s.PublishAgent(t.Context(), "owner", row.ID, &PublishAgentRequest{Title: &updatedTitle, Description: &description, DSL: entity.JSONMap{"exact": json.Number("9007199254740993"), "snapshot": "original"}})
	if err != nil {
		t.Fatal(err)
	}
	var persisted entity.UserCanvas
	if err := db.First(&persisted, "id = ?", row.ID).Error; err != nil || !persisted.Release || *persisted.Title != updatedTitle || *persisted.Description != description {
		t.Fatal("publish overrides not committed", persisted, err)
	}
	if err := s.UpdateAgent(t.Context(), "owner", row.ID, entity.JSONMap{"snapshot": "draft"}); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetVersion(t.Context(), "owner", row.ID, version.ID)
	if err != nil || got.DSL["snapshot"] != "original" {
		t.Fatal("version mutated", got, err)
	}
	if _, err := s.GetVersion(t.Context(), "owner", "other", version.ID); err == nil {
		t.Fatal("version disclosed across canvas")
	}
	if err := s.DeleteVersion(t.Context(), "owner", "other", version.ID); err == nil {
		t.Fatal("version deleted across canvas")
	}
	var raw string
	if err := db.Model(&entity.UserCanvasVersion{}).Where("id = ?", version.ID).Pluck("dsl", &raw).Error; err != nil || !strings.Contains(raw, "9007199254740993") {
		t.Fatal("exact JSON lost", raw, err)
	}
	if events, err := s.RunAgent(t.Context(), "foreign", row.ID, "latest"); events != nil || !errors.Is(err, dao.ErrUserCanvasNotFound) {
		t.Fatal("run bypassed auth", err)
	}
	if events, err := s.RunAgent(t.Context(), "owner", row.ID, "missing"); events != nil || !errors.Is(err, dao.ErrUserCanvasVersionNotFound) {
		t.Fatal("run ignored version", err)
	}
	if events, err := s.RunAgent(t.Context(), "owner", row.ID, "latest"); events != nil || err == nil || !strings.Contains(err.Error(), "not implemented") {
		t.Fatal("run false success", err)
	}
	if err := s.DeleteAgent(t.Context(), "foreign", row.ID); !errors.Is(err, ErrAgentNotOwner) {
		t.Fatal("foreign delete", err)
	}
	if err := s.DeleteAgent(t.Context(), "owner", row.ID); err != nil {
		t.Fatal(err)
	}
	var count int64
	db.Model(&entity.UserCanvasVersion{}).Where("user_canvas_id = ?", row.ID).Count(&count)
	if count != 0 {
		t.Fatal("orphan versions")
	}
}

func TestAgentExecutionIntegrationTransactionRollback(t *testing.T) {
	s, db := targetAgentDB(t)
	title := "rollback"
	row, _, err := s.CreateAgent(t.Context(), &CreateAgentRequest{UserID: "owner", Title: &title, DSL: entity.JSONMap{"before": true}})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`CREATE TRIGGER agent_block_publish BEFORE UPDATE ON user_canvas WHEN NEW.release = 1 BEGIN SELECT RAISE(ABORT, 'forced'); END`).Error; err != nil {
		t.Fatal(err)
	}
	if _, err = s.PublishAgent(t.Context(), "owner", row.ID, nil); err == nil {
		t.Fatal("forced publish succeeded")
	}
	var count int64
	db.Model(&entity.UserCanvasVersion{}).Count(&count)
	if count != 0 {
		t.Fatal("failed publish committed version")
	}
	db.Exec(`DROP TRIGGER agent_block_publish`)
	version, err := s.PublishAgent(t.Context(), "owner", row.ID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`CREATE TRIGGER agent_block_delete BEFORE DELETE ON user_canvas BEGIN SELECT RAISE(ABORT, 'forced'); END`).Error; err != nil {
		t.Fatal(err)
	}
	if err = s.DeleteAgent(t.Context(), "owner", row.ID); err == nil {
		t.Fatal("forced delete succeeded")
	}
	db.Model(&entity.UserCanvasVersion{}).Where("id = ?", version.ID).Count(&count)
	if count != 1 {
		t.Fatal("failed delete lost version")
	}
}

func TestAgentTargetSessionCreationAndCancellation(t *testing.T) {
	s, db := targetAgentDB(t)
	title := "sessions"
	row, _, err := s.CreateAgent(t.Context(), &CreateAgentRequest{UserID: "owner", Title: &title, DSL: entity.JSONMap{"source": "canvas"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{`[`, `[]`, `null`} {
		if _, code, err := s.CreateAgentSession(&CreateAgentSessionRequest{UserID: "owner", AgentID: row.ID, DSL: json.RawMessage(bad)}); err == nil || code == 0 {
			t.Fatal("invalid DSL accepted", bad)
		}
	}
	if _, code, err := s.CreateAgentSession(&CreateAgentSessionRequest{UserID: "foreign", AgentID: row.ID}); err == nil || code == 0 {
		t.Fatal("foreign session accepted")
	}
	session, code, err := s.CreateAgentSession(&CreateAgentSessionRequest{UserID: "owner", AgentID: row.ID, Name: "target", DSL: json.RawMessage(`{"source":"override"}`)})
	if err != nil || code != 0 || session.DSL["source"] != "override" {
		t.Fatal(session, code, err)
	}
	var stored entity.API4Conversation
	if err := db.First(&stored, "id = ?", session.ID).Error; err != nil || stored.DialogID != row.ID || stored.UserID != "owner" || string(stored.Message) != "[]" {
		t.Fatal("session not persisted", stored, err)
	}
	cancel := make(chan struct{})
	s.runStreams[row.ID] = cancel
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := s.CancelAgent(context.Background(), "owner", row.ID); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	select {
	case <-cancel:
	default:
		t.Fatal("cancel was not signaled")
	}
	if _, code, err := s.GetAgentSession("foreign", row.ID, session.ID); err == nil || code == 0 {
		t.Fatal("foreign session disclosed")
	}
	if ok, _, err := s.DeleteAgentSessionItem("owner", row.ID, session.ID); err != nil || !ok {
		t.Fatal(err)
	}
	db.Model(&entity.API4Conversation{}).Where("id = ?", session.ID).Count(new(int64))
}
