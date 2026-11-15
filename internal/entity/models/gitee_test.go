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

package models

import (
	"encoding/json"
	"io"
	"net/http"

	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func initProviderManagerWithGiteeForTest(t *testing.T) {
	t.Helper()

	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "gitee.json"), readProviderConfig(t, "gitee.json"), 0o600); err != nil {
		t.Fatalf("write gitee config: %v", err)
	}
	if err := InitProviderManager(dir); err != nil {
		t.Fatalf("InitProviderManager: %v", err)
	}
}

func newGiteeForListModelsTest(baseURL string) *GiteeModel {
	return NewGiteeModel(map[string]string{"default": baseURL}, URLSuffix{Models: "models"})
}

func deepSeekAliasModelsForTest(t *testing.T) map[string]Model {
	t.Helper()

	pm := GetProviderManager()
	if pm == nil {
		t.Fatal("provider manager is nil")
	}

	aliasModels := map[string]Model{}
	for _, model := range pm.AllModels {
		if !strings.HasPrefix(model.Name, "deepseek-") {
			continue
		}
		for _, alias := range model.Alias {
			if alias == "" {
				continue
			}
			if existing, ok := aliasModels[alias]; ok {
				t.Fatalf("duplicate DeepSeek alias %q for %q and %q", alias, existing.Name, model.Name)
			}
			aliasModels[alias] = model
		}
	}
	if len(aliasModels) == 0 {
		t.Fatal("no DeepSeek aliases found in all_models.json")
	}
	return aliasModels
}

func TestGiteeListModelsMapsAllDeepSeekAliasesToModelMetadata(t *testing.T) {
	initProviderManagerWithGiteeForTest(t)
	aliasModels := deepSeekAliasModelsForTest(t)
	aliases := make([]string, 0, len(aliasModels))
	for alias := range aliasModels {
		aliases = append(aliases, alias)
	}
	sort.Strings(aliases)

	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method=%s, want GET", r.Method)
		}
		if r.URL.Path != "/models" {
			t.Errorf("path=%s, want /models", r.URL.Path)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Errorf("Content-Type=%q, want application/json", got)
		}

		resp := ModelList{
			Object: "list",
			Models: make([]DSModel, 0, len(aliases)+1),
		}
		for _, alias := range aliases {
			resp.Models = append(resp.Models, DSModel{ID: alias})
		}
		resp.Models = append(resp.Models, DSModel{ID: "unknown-model"})

		w.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(w).Encode(resp); err != nil {
			t.Fatalf("encode response: %v", err)
		}
	}))
	defer srv.Close()

	models, err := newGiteeForListModelsTest(srv.URL).ListModels(&APIConfig{})
	if err != nil {
		t.Fatalf("ListModels: %v", err)
	}
	if got, want := len(models), len(aliases)+1; got != want {
		t.Fatalf("len(models)=%d, want %d", got, want)
	}

	for i, alias := range aliases {
		model := models[i]
		expected := aliasModels[alias]
		if model.Name != alias {
			t.Fatalf("models[%d].Name=%q, want %q", i, model.Name, alias)
		}
		if (model.MaxTokens == nil) != (expected.MaxTokens == nil) {
			t.Fatalf("models[%d] alias %q MaxTokens nil=%t, want nil=%t", i, alias, model.MaxTokens == nil, expected.MaxTokens == nil)
		}
		if model.MaxTokens != nil && expected.MaxTokens != nil && *model.MaxTokens != *expected.MaxTokens {
			t.Fatalf("models[%d] alias %q MaxTokens=%d, want %d", i, alias, *model.MaxTokens, *expected.MaxTokens)
		}
		if strings.Join(model.ModelTypes, ",") != strings.Join(expected.ModelTypes, ",") {
			t.Fatalf("models[%d] alias %q ModelTypes=%v, want %v", i, alias, model.ModelTypes, expected.ModelTypes)
		}
		if (model.Thinking == nil) != (expected.Thinking == nil) {
			t.Fatalf("models[%d] alias %q Thinking nil=%t, want nil=%t", i, alias, model.Thinking == nil, expected.Thinking == nil)
		}
	}

	unknown := models[len(models)-1]
	if unknown.Name != "unknown-model" {
		t.Fatalf("unknown.Name=%q, want unknown-model", unknown.Name)
	}
	if unknown.MaxTokens != nil {
		t.Fatalf("unknown.MaxTokens=%v, want nil", *unknown.MaxTokens)
	}
	if len(unknown.ModelTypes) != 0 {
		t.Fatalf("unknown.ModelTypes=%v, want empty", unknown.ModelTypes)
	}
}

func TestGiteeListModelsKeepsOwnedBySuffixAfterAliasMetadataLookup(t *testing.T) {
	initProviderManagerWithGiteeForTest(t)

	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"object":"list","data":[{"id":"deepseek/deepseek-v4-pro","owned_by":"gitee"}]}`)
	}))
	defer srv.Close()

	models, err := newGiteeForListModelsTest(srv.URL).ListModels(&APIConfig{})
	if err != nil {
		t.Fatalf("ListModels: %v", err)
	}
	if len(models) != 1 {
		t.Fatalf("len(models)=%d, want 1", len(models))
	}
	model := models[0]
	if model.Name != "deepseek/deepseek-v4-pro@gitee" {
		t.Fatalf("Name=%q, want deepseek/deepseek-v4-pro@gitee", model.Name)
	}
	if model.MaxTokens == nil || *model.MaxTokens != 1048576 {
		t.Fatalf("MaxTokens=%v, want 1048576", model.MaxTokens)
	}
	if len(model.ModelTypes) != 1 || model.ModelTypes[0] != "chat" {
		t.Fatalf("ModelTypes=%v, want [chat]", model.ModelTypes)
	}
	if model.Thinking == nil {
		t.Fatal("Thinking=nil, want DeepSeek V4 Pro thinking metadata")
	}
}

func TestGiteeListModelsControlledRoundTrip(t *testing.T) {
	initProviderManagerWithGiteeForTest(t)
	aliases := deepSeekAliasModelsForTest(t)
	list := ModelList{}
	for alias := range aliases {
		list.Models = append(list.Models, DSModel{ID: alias})
	}
	if len(list.Models) == 0 {
		t.Fatal("frozen catalog contains no DeepSeek aliases")
	}
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/models" {
			t.Error("model discovery route changed", r.Method, r.URL.Path)
		}
		json.NewEncoder(w).Encode(list)
	}))
	rows, err := newGiteeForListModelsTest(server.URL).ListModels(&APIConfig{})
	if err != nil || len(rows) != len(list.Models) {
		t.Fatal("controlled discovery failed", len(rows), err)
	}
	matched := 0
	for _, row := range rows {
		if len(row.ModelTypes) > 0 {
			matched++
		}
	}
	if matched != len(list.Models) {
		t.Fatal("typed catalog metadata was not attached", rows)
	}
}
