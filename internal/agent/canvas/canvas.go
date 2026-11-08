// Package canvas compiles supported v1 agent graphs into Eino workflows.
// Python remains the production runtime; unsupported Go semantics fail explicitly.
package canvas

import (
	"multirag/internal/agent/runtime"
)

var legacyNoOpNames = map[string]bool{
	"exitloop": true,
}

type CanvasState = runtime.CanvasState

func NewCanvasState(runID, taskID string) *CanvasState {
	return runtime.NewCanvasState(runID, taskID)
}

type Canvas struct {
	Version    int                        `json:"version"`
	Components map[string]CanvasComponent `json:"components"`
	Path       []string                   `json:"path"`
	History    []map[string]any           `json:"history,omitempty"`
	Retrieval  map[string]any             `json:"retrieval,omitempty"`
	Globals    map[string]any             `json:"globals,omitempty"`
}

type CanvasComponent struct {
	Obj        CanvasComponentObj `json:"obj"`
	Downstream []string           `json:"downstream"`
	Upstream   []string           `json:"upstream"`
}

type CanvasComponentObj struct {
	ComponentName string         `json:"component_name"`
	Params        map[string]any `json:"params"`
}

type Component = runtime.Component

type ComponentFactory = runtime.ComponentFactory

func SetDefaultFactory(f ComponentFactory) {
	runtime.SetDefaultFactory(f)
}
