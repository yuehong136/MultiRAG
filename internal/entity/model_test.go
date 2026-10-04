package entity

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func TestProviderModelClassWithoutHyphen(t *testing.T) {
	directory := t.TempDir()
	config := `{"name":"DeepSeek","url":{"default":"https://example.invalid"},"models":[{"name":"plain","model_types":["chat"]}]}`
	if err := os.WriteFile(filepath.Join(directory, "provider.json"), []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	manager, err := NewProviderManager(directory)
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}
	model, err := manager.GetModelByName("DeepSeek", "plain")
	if err != nil || model.Class == nil || *model.Class != "plain" {
		t.Fatalf("model without hyphen = %#v, %v", model, err)
	}
}

func TestProviderModelsKeepInitializedTypeMaps(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	model, err := manager.GetModelByName("zhipu-ai", "glm-4.6v-flash")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if !model.ModelTypeMap["chat"] || !model.ModelTypeMap["vision"] {
		t.Fatalf("ModelTypeMap = %#v, want chat and vision", model.ModelTypeMap)
	}

	provider := manager.FindProvider("zhipu-ai")
	if provider == nil {
		t.Fatal("FindProvider() returned nil")
	}
	if got := provider.URL["default"]; got != "https://open.bigmodel.cn/api/paas/v4" {
		t.Fatalf("default provider URL = %q", got)
	}
}

func TestProviderModelsResolveThinkingFeatures(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	thinkingModel, err := manager.GetModelByName("zhipu-ai", "glm-4.5-air")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if thinkingModel.Thinking == nil || !thinkingModel.Thinking.DefaultValue || !thinkingModel.Thinking.ClearThinking {
		t.Fatalf("Thinking = %#v", thinkingModel.Thinking)
	}

	plainModel, err := manager.GetModelByName("zhipu-ai", "glm-4-plus")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if plainModel.Thinking != nil {
		t.Fatalf("Thinking = %#v, want nil", plainModel.Thinking)
	}

	models, err := manager.ListModels("zhipu-ai")
	if err != nil {
		t.Fatalf("ListModels() error = %v", err)
	}
	for _, model := range models {
		if model["name"] == "glm-4.7" {
			features, ok := model["features"].([]string)
			if !ok || !slices.Contains(features, "thinking") {
				t.Fatalf("glm-4.7 features = %#v", model["features"])
			}
			return
		}
	}
	t.Fatal("glm-4.7 not found")
}

// SHOW BALANCE 依赖 provider 配置里的 balance suffix：Moonshot 的驱动早已实现
// Balance()，但在补上这份配置之前 FindProvider 返回 nil，命令直接 404。
func TestMoonshotProviderSupportsBalanceLookup(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	provider := manager.FindProvider("moonshot")
	if provider == nil {
		t.Fatal("FindProvider(\"moonshot\") returned nil")
	}
	if got := provider.URL["default"]; got != "https://api.moonshot.cn/v1" {
		t.Fatalf("default provider URL = %q", got)
	}
	if got := provider.URLSuffix.Balance; got != "users/me/balance" {
		t.Fatalf("balance suffix = %q, want users/me/balance", got)
	}
	if got := provider.URLSuffix.Models; got != "models" {
		t.Fatalf("models suffix = %q, want models", got)
	}

	model, err := manager.GetModelByName("moonshot", "kimi-k2.6")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if model.Thinking == nil || !model.Thinking.ClearThinking {
		t.Fatalf("kimi-k2.6 thinking = %#v, want clear_thinking enabled", model.Thinking)
	}
}

func TestDeepSeekProviderIsConfigured(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	provider := manager.FindProvider("deepseek")
	if provider == nil {
		t.Fatal("FindProvider(\"deepseek\") returned nil")
	}
	if got := provider.URL["default"]; got != "https://api.deepseek.com" {
		t.Fatalf("default provider URL = %q", got)
	}

	model, err := manager.GetModelByName("deepseek", "deepseek-v4-pro")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if !model.ModelTypeMap["chat"] {
		t.Fatalf("ModelTypeMap = %#v, want chat", model.ModelTypeMap)
	}
	if model.Class == nil || *model.Class != "deepseek" {
		t.Fatalf("Class = %#v, want deepseek", model.Class)
	}
}

func TestGiteeAndSiliconFlowProvidersAreConfigured(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	for _, testCase := range []struct {
		provider string
		model    string
	}{
		{provider: "gitee", model: "qwen3-8b"},
		{provider: "siliconflow", model: "qwen/qwen3-8b"},
	} {
		provider := manager.FindProvider(testCase.provider)
		if provider == nil || provider.ModelDriver.Name() != testCase.provider {
			t.Fatalf("provider %q = %#v", testCase.provider, provider)
		}
		model, modelErr := manager.GetModelByName(testCase.provider, testCase.model)
		if modelErr != nil || !model.ModelTypeMap["chat"] {
			t.Fatalf("model %q/%q = %#v, %v", testCase.provider, testCase.model, model, modelErr)
		}
	}
}

func TestAliyunProviderIsConfigured(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}
	provider := manager.FindProvider("Aliyun")
	if provider == nil || provider.ModelDriver.Name() != "aliyun" {
		t.Fatalf("Aliyun provider = %#v", provider)
	}
	if provider.URL["singapore"] != "https://dashscope-intl.aliyuncs.com" {
		t.Fatalf("Aliyun Singapore URL = %q", provider.URL["singapore"])
	}
	if provider.URLSuffix.Chat != "compatible-mode/v1/chat/completions" {
		t.Fatalf("Aliyun chat suffix = %q", provider.URLSuffix.Chat)
	}
	model, err := manager.GetModelByName("Aliyun", "qwen-flash")
	if err != nil || !model.ModelTypeMap["chat"] {
		t.Fatalf("Aliyun model = %#v, %v", model, err)
	}
	if model.Class == nil || *model.Class != "qwen" {
		t.Fatalf("Aliyun model class = %#v", model.Class)
	}
	if model.Thinking == nil || !model.Thinking.DefaultValue {
		t.Fatalf("Aliyun thinking feature = %#v", model.Thinking)
	}
}

func TestMinimaxProviderIsConfigured(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	provider := manager.FindProvider("minimax")
	if provider == nil {
		t.Fatal("FindProvider(\"minimax\") returned nil")
	}
	if got := provider.URL["global"]; got != "https://api.minimax.io/" {
		t.Fatalf("global provider URL = %q", got)
	}
	if got := provider.URLSuffix.Files; got != "v1/files/list" {
		t.Fatalf("files suffix = %q", got)
	}

	model, err := manager.GetModelByName("minimax", "minimax-m2.7")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if !model.ModelTypeMap["chat"] || model.Thinking == nil || !model.Thinking.DefaultValue {
		t.Fatalf("MiniMax model = %#v", model)
	}
}

func TestModelLevelThinkingAndGoogleFactory(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		provider, model string
		clear           bool
	}{
		{"Google", "gemini-2.5-flash", true}, {"DeepSeek", "deepseek-v4-pro", true},
		{"Moonshot", "kimi-k2.5", true}, {"ZHIPU-AI", "glm-5-turbo", true},
		{"ZHIPU-AI", "glm-4.7-flashx", true}, {"MiniMax", "minimax-m2.7", false},
	} {
		model, err := manager.GetModelByName(test.provider, test.model)
		if err != nil || model.Thinking == nil || !model.Thinking.DefaultValue || model.Thinking.ClearThinking != test.clear {
			t.Fatalf("%s/%s: %#v %v", test.provider, test.model, model, err)
		}
	}
	provider := manager.FindProvider("Google")
	if provider == nil || provider.ModelDriver.Name() != "google" {
		t.Fatal("Google not wired to factory")
	}
	if _, err := manager.GetModelByName("ZHIPU-AI", "glm-5.1"); err == nil {
		t.Fatal("target removed model still advertised")
	}
	for _, provider := range []string{"Gitee", "SiliconFlow"} {
		for _, model := range manager.FindProvider(provider).Models {
			if model.Thinking != nil {
				t.Fatalf("stale provider thinking applied to %s/%s", provider, model.Name)
			}
		}
	}
}

func TestExplicitModelThinkingTakesPrecedenceOverLegacyProviderDefaults(t *testing.T) {
	directory := t.TempDir()
	config := `{"name":"Google","url":{"default":"https://example.invalid"},"models":[{"name":"gemini-test","model_types":["chat"],"thinking":{"default_value":false,"clear_thinking":false}}],"features":{"thinking":{"default_value":true,"supported_models":["gemini"]}}}`
	if err := os.WriteFile(filepath.Join(directory, "google.json"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	manager, err := NewProviderManager(directory)
	if err != nil {
		t.Fatal(err)
	}
	model, err := manager.GetModelByName("Google", "gemini-test")
	if err != nil || model.Thinking == nil || model.Thinking.DefaultValue {
		t.Fatalf("explicit model default overwritten: %v %v", model, err)
	}
}

func TestVolcEngineModelThinkingDefaults(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	model, err := manager.GetModelByName("VolcEngine", "doubao-seed-2-0-pro-260215")
	if err != nil || model.Thinking == nil || !model.Thinking.DefaultValue || !model.Thinking.ClearThinking {
		t.Fatalf("thinking: %v %v", model, err)
	}
}
