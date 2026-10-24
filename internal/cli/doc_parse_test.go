package cli

import (
	"reflect"
	"testing"
)

func TestDocParseModelDeclaration(t *testing.T) {
	cmd, err := NewParser("ADD MODEL 'pipeline' TO PROVIDER 'mineru_local' INSTANCE 'one' WITH DOC_PARSE TOKENS 8192;").Parse(false)
	if err != nil || cmd.Params["model_type"] != "doc_parse" || !reflect.DeepEqual(cmd.Params["model_types"], []string{"doc_parse"}) {
		t.Fatal(cmd, err)
	}
	for _, suffix := range []string{"DOC_PARSE DOC_PARSE TOKENS 1;", "DOC_PARSE THINK TOKENS 1;", "DOC_PARSE TOKENS 0;", "DOC_PARSE TOKENS 1; ignored"} {
		if _, err := NewParser("ADD MODEL 'pipeline' TO PROVIDER 'mineru_local' INSTANCE 'one' WITH " + suffix).Parse(false); err == nil {
			t.Fatal(suffix)
		}
	}
}
