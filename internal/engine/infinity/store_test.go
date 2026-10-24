package infinity

import (
	"encoding/binary"
	sdk "github.com/infiniflow/infinity-go-sdk"
	"math"
	"reflect"
	"strings"
	"testing"
)

func TestStrictStoreFiltersDoNotDropScope(t *testing.T) {
	columns := map[string]struct {
		Type    string
		Default interface{}
	}{"id": {"Varchar", ""}, "kb_id": {"Varchar", ""}, "available_int": {"Integer", 1}, "tag_kwd": {"Varchar", ""}}
	got, err := strictFilterFromCondition(map[string]interface{}{"id": []string{"chunk-a", "O'Brien"}, "available_int": 0}, columns)
	if err != nil || !strings.Contains(got, `id IN ('chunk-a', "O'Brien")`) || !strings.Contains(got, "available_int = 0") {
		t.Fatal(got, err)
	}
	for _, bad := range []map[string]interface{}{{"id": []string{}}, {"id": nil}, {"id": []byte("x")}, {"id": struct{}{}}, {"unknown": "x"}, {"available_int": math.NaN()}, {"must_not": map[string]interface{}{"unknown": "id"}}} {
		if filter, err := strictFilterFromCondition(bad, columns); err == nil {
			t.Fatal("condition broadened", bad, filter)
		}
	}
}
func TestMetadataRejectsMalformedObjects(t *testing.T) {
	for _, raw := range []interface{}{`null`, `[]`, `{"broken"`, 4} {
		if _, err := decodeMetadataFields(raw); err == nil {
			t.Fatal(raw)
		}
	}
	if got, err := decodeMetadataFields(`{"zero":0,"keep":false}`); err != nil || got["zero"] != float64(0) || got["keep"] != false {
		t.Fatal(got, err)
	}
}

func TestSDKFilterLiteralAndMetadataFrames(t *testing.T) {
	literal, err := filterLiteral("O'Brien")
	if err != nil {
		t.Fatal(err)
	}
	expr, err := sdk.ParseFilter("id = " + literal)
	if err != nil {
		t.Fatal(err)
	}
	if got := *expr.Type.FunctionExpr.Arguments[1].Type.ConstantExpr.StrValue; got != "O'Brien" {
		t.Fatalf("SDK changed literal: %q", got)
	}
	if _, err := filterLiteral("both'and\""); err == nil {
		t.Fatal("unrepresentable literal accepted")
	}
	frames := []byte{}
	for _, raw := range []string{`{"first":1}`, `{"second":0}`} {
		frames = binary.LittleEndian.AppendUint32(frames, uint32(len(raw)))
		frames = append(frames, raw...)
	}
	rows, err := decodeMetadataColumn([]interface{}{frames}, 2)
	if err != nil || !reflect.DeepEqual(rows, []interface{}{map[string]interface{}{"first": float64(1)}, map[string]interface{}{"second": float64(0)}}) {
		t.Fatal(rows, err)
	}
	for _, raw := range [][]byte{frames[:len(frames)-1], {1, 0}, binary.LittleEndian.AppendUint32(nil, 100)} {
		if _, err := decodeMetadataColumn([]interface{}{raw}, 2); err == nil {
			t.Fatal("invalid frames accepted")
		}
	}
}
