package handler

import (
	"multirag/internal/service"
	"testing"
)

func TestCustomModelRouteIdentity(t *testing.T) {
	request := &service.AddCustomModelRequest{ModelName: "model", ModelTypes: []string{"chat"}, MaxTokens: 0}
	if err := prepareCustomModelRequest(request, "OpenAI", "instance"); err != nil || request.ProviderName != "OpenAI" || request.InstanceName != "instance" {
		t.Fatalf("path identity was not applied: %v", err)
	}
	for _, request := range []*service.AddCustomModelRequest{nil, {ProviderName: "Other", ModelName: "model", ModelTypes: []string{"chat"}}, {InstanceName: "other", ModelName: "model", ModelTypes: []string{"chat"}}, {ModelName: "model", ModelTypes: []string{}}, {ModelName: "model"}, {ModelName: "   ", ModelTypes: []string{"chat"}}} {
		if err := prepareCustomModelRequest(request, "OpenAI", "instance"); err == nil {
			t.Fatal("invalid route/declaration accepted")
		}
	}
}
