package service

import (
	"multirag/internal/entity/models"
	"testing"
)

func TestModelChatDefaultsPreserveExplicitThinking(t *testing.T) {
	model := &models.Model{Thinking: &models.ModelThinking{DefaultValue: true}}
	config := &models.ChatConfig{}
	applyModelChatDefaults(model, config)
	if config.Thinking == nil || !*config.Thinking {
		t.Fatal("missing model thinking default")
	}
	explicitFalse := false
	config.Thinking = &explicitFalse
	applyModelChatDefaults(model, config)
	if *config.Thinking {
		t.Fatal("explicit false was overwritten")
	}
	config = &models.ChatConfig{}
	model.Thinking = nil
	applyModelChatDefaults(model, config)
	if config.Thinking != nil {
		t.Fatal("thinking enabled for unsupported model")
	}
}
