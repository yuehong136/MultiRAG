package entity

import (
	"testing"

	"multirag/internal/entity/models"
)

func TestBaiduCatalogRegistration(t *testing.T) {
	manager := newCatalogManager(t)
	requireCatalogProvider(t, manager, "Baidu", (*models.BaiduModel)(nil), models.URLSuffix{
		Chat: "chat/completions", Models: "models", Embedding: "embeddings", Rerank: "rerank", OCR: "ocr/paddleocr",
	})
	// OCR extends the catalog; the original chat, vision, embedding and rerank
	// contracts still have to resolve by name, with thinking opted in per model.
	requireCatalogModels(t, manager, "Baidu", []catalogModelContract{
		{"deepseek-v3.2", []string{"chat"}, nil},
		{"deepseek-v4-flash", []string{"chat"}, &ModelThinking{DefaultValue: true, ClearThinking: true}},
		{"deepseek-v4-pro", []string{"chat"}, &ModelThinking{DefaultValue: true, ClearThinking: true}},
		{"ernie-5.0", []string{"vision"}, nil},
		{"embedding-v1", []string{"embedding"}, nil},
		{"qwen3-reranker-4b", []string{"rerank"}, nil},
		{"paddleocr-vl-0.9b", []string{"ocr"}, nil},
	})
}
