package cli

import (
	"reflect"
	"testing"
)

func TestObjectFieldRowsStableAndLossless(t *testing.T) {
	data := map[string]interface{}{"currency": "CNY", "balance": float64(0), "nested": map[string]interface{}{"available": false}}
	rows := objectFieldRows(data)
	if len(rows) != 3 || rows[0]["field"] != "balance" || rows[1]["field"] != "currency" || rows[2]["field"] != "nested" || rows[0]["value"] != float64(0) || !reflect.DeepEqual(rows[2]["value"], data["nested"]) {
		t.Fatal(rows)
	}
	if len(data) != 3 || data["balance"] != float64(0) {
		t.Fatal("object changed")
	}
	if len(objectFieldRows(nil)) != 0 {
		t.Fatal("empty object acquired fields")
	}
}

func TestCustomModelZeroTokenLimit(t *testing.T) {
	cmd, err := NewParser(`ADD MODEL 'zero' TO PROVIDER 'vllm' INSTANCE 'local' WITH TOKENS 0 CHAT;`).Parse(false)
	if err != nil || cmd.Params["models"].([]customModelDeclaration)[0].MaxTokens != 0 {
		t.Fatalf("zero token limit: %v %v", cmd, err)
	}
}
