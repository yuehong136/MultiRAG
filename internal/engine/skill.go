package engine

import (
	"context"
	"multirag/internal/engine/types"
)

// SkillDocEngine is the narrow upstream document contract used by Skills.
// Dataset engines with unimplemented writes cannot satisfy this contract.
type SkillDocEngine interface {
	GetType() string
	Search(context.Context, *types.SearchRequest) (*types.SearchResult, error)
	CreateDataset(context.Context, string, string, int, string) error
	DropTable(context.Context, string) error
	TableExists(context.Context, string) (bool, error)
	IndexDocument(context.Context, string, string, map[string]interface{}) error
	DeleteDocument(context.Context, string, string) error
}

// SkillIndexReplacer builds and verifies a replacement before publishing it.
type SkillIndexReplacer interface {
	ReplaceSkillIndex(context.Context, string, int, func(SkillDocEngine) error) error
}
