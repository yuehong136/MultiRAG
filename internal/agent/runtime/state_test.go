package runtime

import (
	"encoding/json"
	"testing"
)

func TestCanvasCheckpointPreservesExactNumbers(t *testing.T) {
	const exact = "9007199254740993"
	state := NewCanvasState("run", "task")
	state.SetVar("model", "id", json.Number(exact))
	state.SetSys("model_id", json.Number(exact))
	state.SetGlobal("exact", json.Number(exact))
	raw, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	restored := NewCanvasState("", " ")
	if err := json.Unmarshal(raw, restored); err != nil {
		t.Fatal(err)
	}
	value, err := restored.GetVar("model@id")
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]any{"output": value, "sys": restored.GetSys("model_id"), "global": restored.Globals["exact"]} {
		number, ok := value.(json.Number)
		if !ok || number.String() != exact {
			t.Fatalf("%s rounded: %v (%T)", name, value, value)
		}
	}
}
