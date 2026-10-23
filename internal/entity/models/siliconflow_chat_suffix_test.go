package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestSiliconFlowConfiguredChatSuffixHTTP(t *testing.T) {
	calls := 0
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/instance/custom/chat" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("configured endpoint was not honored", r.URL.Path)
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["stream"] == true {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
		} else {
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
		}
	}))
	defer fixture.Close()
	d := NewSiliconFlowModel(map[string]string{"default": fixture.URL + "/instance/"}, URLSuffix{Chat: "/custom/chat"})
	api := &APIConfig{APIKey: stringPtr("key")}
	response, err := d.ChatWithMessages("model", api, []Message{{Role: "user", Content: "q"}}, nil)
	if err != nil || response == nil || *response.Answer != "answer" {
		t.Fatal(response, err)
	}
	if err := d.ChatStreamlyWithSender(stringPtr("model"), stringPtr("q"), api, nil, func(*string, *string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatal("chat requests missing", calls)
	}
}
