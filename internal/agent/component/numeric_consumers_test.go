package component

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"math"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/xuri/excelize/v2"

	"multirag/internal/agent/canvas"
)

// Decode the same way as the public run endpoint and checkpoint state, rather
// than constructing float64 fixtures that would hide a UseNumber regression.
func numericJSON(t *testing.T, text string) map[string]any {
	t.Helper()
	var value map[string]any
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func TestNumericAssignerJSONExactArithmetic(t *testing.T) {
	for _, tc := range []struct{ initial, operator, parameter, want string }{
		{"9007199254740993", "+=", "2", "9007199254740995"},
		{"9007199254740993", "-=", "2", "9007199254740991"},
		{"0.1", "+=", "0.2", "0.3"},
		{"0.1", "*=", "0.2", "0.02"},
		{"0.3", "/=", "0.1", "3"},
		{"1", "/=", "8", "0.125"},
		{"1", "/=", "3", "0.3333333333333333"},
		{"0.1", "/=", "0.3", "0.33333333333333337"},
		{"9007199254740993", "/=", "3", "3002399751580331"},
		{"9007199254740993", "/=", "2", "4503599627370496.5"},
		{"9", "clear", "null", "0"},
	} {
		t.Run(tc.initial+tc.operator+tc.parameter, func(t *testing.T) {
			state := canvas.NewCanvasState("numeric", "numeric")
			state.Sys["n"] = numericJSON(t, `{"n":`+tc.initial+`}`)["n"]
			params := numericJSON(t, fmt.Sprintf(`{"variables":[{"variable":"sys.n","operator":%q,"parameter":%s}]}`, tc.operator, tc.parameter))
			for _, runtimeOverride := range []bool{false, true} {
				state.Sys["n"] = numericJSON(t, `{"n":`+tc.initial+`}`)["n"]
				component, err := NewVariableAssignerComponent(params)
				if err != nil {
					t.Fatal(err)
				}
				var inputs map[string]any
				if runtimeOverride {
					inputs = params
				}
				out, err := component.Invoke(canvas.WithState(context.Background(), state), inputs)
				if err != nil || out["errors"] != nil || state.Sys["n"] != json.Number(tc.want) {
					t.Fatalf("override=%v value=%#v out=%v err=%v", runtimeOverride, state.Sys["n"], out, err)
				}
			}
		})
	}
}

func TestNumericAssignerFiniteFloatArithmetic(t *testing.T) {
	for _, tc := range []struct {
		name, operator     string
		initial, parameter any
		left, right        float64
	}{
		{"integer_third", "/=", 1, 3, 1, 3},
		{"decimal_third", "/=", 0.1, 0.3, 0.1, 0.3},
		{"decimal_finite_quotient", "/=", 0.3, 0.1, 0.3, 0.1},
		{"decimal_sum", "+=", 0.1, 0.2, 0.1, 0.2},
		{"decimal_product", "*=", 0.1, 0.2, 0.1, 0.2},
		{"decimal_subtract", "-=", 0.3, 0.1, 0.3, 0.1},
		{"negative_third", "/=", -1.0, 3.0, -1, 3},
		{"float32_third", "/=", float32(0.1), float32(0.3), float64(float32(0.1)), float64(float32(0.3))},
	} {
		t.Run(tc.name, func(t *testing.T) {
			want := 0.0
			switch tc.operator {
			case "+=":
				want = tc.left + tc.right
			case "-=":
				want = tc.left - tc.right
			case "*=":
				want = tc.left * tc.right
			case "/=":
				want = tc.left / tc.right
			}
			for _, override := range []bool{false, true} {
				state := canvas.NewCanvasState("numeric", "numeric")
				state.Sys["n"] = tc.initial
				params := map[string]any{"variables": []any{map[string]any{"variable": "sys.n", "operator": tc.operator, "parameter": tc.parameter}}}
				component, err := NewVariableAssignerComponent(params)
				if err != nil {
					t.Fatal(err)
				}
				var input map[string]any
				if override {
					input = params
				}
				out, err := component.Invoke(canvas.WithState(context.Background(), state), input)
				if err != nil || out["errors"] != nil || state.Sys["n"] != want {
					t.Fatalf("override=%v original=%0.17g current=%#v out=%v err=%v", override, want, state.Sys["n"], out, err)
				}
			}
		})
	}
}

func TestNumericAssignerRejectsLossWithoutMutation(t *testing.T) {
	for _, tc := range []struct{ initial, operator, parameter, marker string }{
		{"1e308", "*=", "10", "NUMERIC_PRECISION_OR_RANGE"},
		{"1e-323", "/=", "100", "NUMERIC_PRECISION_OR_RANGE"},
		{"1", "+=", "1e1000", "INVALID_NUMBER"},
		{"1", "+=", "1e-1000", "INVALID_NUMBER"},
		{"1", "/=", "0", "DIVIDE_BY_ZERO"},
	} {
		t.Run(tc.initial+tc.operator+tc.parameter, func(t *testing.T) {
			state := canvas.NewCanvasState("numeric", "numeric")
			initial := numericJSON(t, `{"n":`+tc.initial+`}`)["n"]
			state.Sys["n"] = initial
			params := numericJSON(t, fmt.Sprintf(`{"variables":[{"variable":"sys.n","operator":%q,"parameter":%s}]}`, tc.operator, tc.parameter))
			component, err := NewVariableAssignerComponent(params)
			if err != nil {
				t.Fatal(err)
			}
			out, err := component.Invoke(canvas.WithState(context.Background(), state), nil)
			if err != nil || !strings.Contains(fmt.Sprint(out["errors"]), tc.marker) || state.Sys["n"] != initial || len(out["assignments"].([]string)) != 0 {
				t.Fatalf("value=%#v out=%v err=%v", state.Sys["n"], out, err)
			}
		})
	}
	for _, invalid := range []any{json.Number("NaN"), json.Number("01"), math.Inf(1), math.NaN()} {
		state := canvas.NewCanvasState("numeric", "numeric")
		state.Sys["n"] = json.Number("1")
		component, err := NewVariableAssignerComponent(map[string]any{"variables": []any{map[string]any{"variable": "sys.n", "operator": "+=", "parameter": invalid}}})
		if err != nil {
			t.Fatal(err)
		}
		out, err := component.Invoke(canvas.WithState(context.Background(), state), nil)
		if err != nil || !strings.Contains(fmt.Sprint(out["errors"]), "INVALID_NUMBER") || state.Sys["n"] != json.Number("1") {
			t.Fatalf("invalid=%v out=%v err=%v", invalid, out, err)
		}
	}
}

func TestNumericListJSONIndexSortFilterAndDedup(t *testing.T) {
	for _, tc := range []struct{ config, items, want string }{
		{`"operations":"nth","n":2`, `[10,2,3]`, `[2]`},
		{`"operations":"nth","n":-2`, `[10,2,3]`, `[2]`},
		{`"operations":"head","n":2`, `[10,2,3]`, `[10,2]`},
		{`"operations":"tail","n":2`, `[10,2,3]`, `[2,3]`},
		{`"operations":"sort"`, `[10,2,9007199254740993,9007199254740992]`, `[2,10,9007199254740992,9007199254740993]`},
		{`"operations":"sort","sort_method":"desc"`, `[10,2,9007199254740992,9007199254740993]`, `[9007199254740993,9007199254740992,10,2]`},
		{`"operations":"sort"`, `[2,"15",10]`, `[2,10,"15"]`},
		{`"operations":"drop_duplicates"`, `[1,1.0,9007199254740993,9007199254740992]`, `[1,9007199254740993,9007199254740992]`},
		{`"operations":"filter","filter":{"operator":"=","value":1.0}`, `[1,2,1.0]`, `[1,1.0]`},
	} {
		t.Run(tc.config, func(t *testing.T) {
			state := canvas.NewCanvasState("numeric", "numeric")
			state.Sys["items"] = numericJSON(t, `{"items":`+tc.items+`}`)["items"]
			component, err := NewListOperationsComponent(numericJSON(t, `{"query":"sys.items",`+tc.config+`}`))
			if err != nil {
				t.Fatal(err)
			}
			out, err := component.Invoke(canvas.WithState(context.Background(), state), nil)
			if err != nil || !reflect.DeepEqual(out["result"], numericJSON(t, `{"items":`+tc.want+`}`)["items"]) {
				t.Fatalf("out=%v err=%v", out, err)
			}
		})
	}
	for _, n := range []string{"2.5", "9223372036854775808", "1e1000", `"2trailing"`} {
		if _, err := NewListOperationsComponent(numericJSON(t, `{"query":"sys.items","n":`+n+`}`)); err == nil {
			t.Fatalf("accepted invalid index %s", n)
		}
	}
	state := canvas.NewCanvasState("numeric", "numeric")
	state.Sys["items"] = []any{json.Number("NaN"), json.Number("2")}
	component, err := NewListOperationsComponent(numericJSON(t, `{"query":"sys.items","operations":"sort"}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := component.Invoke(canvas.WithState(context.Background(), state), nil); err == nil {
		t.Fatal("invalid number fell back to lexical sorting")
	}
}

func TestNumericAggregatorJSONZeroAndInvalid(t *testing.T) {
	state := canvas.NewCanvasState("numeric", "numeric")
	for name, value := range numericJSON(t, `{"zero":0,"negativeZero":-0,"exponentZero":0e2,"selected":9007199254740993}`) {
		state.Sys[name] = value
	}
	params := numericJSON(t, `{"groups":[{"group_name":"chosen","variables":[{"value":"sys.zero"},{"value":"sys.negativeZero"},{"value":"sys.exponentZero"},{"value":"sys.selected"}]}]}`)
	component, err := NewVariableAggregatorComponent(params)
	if err != nil {
		t.Fatal(err)
	}
	out, err := component.Invoke(canvas.WithState(context.Background(), state), nil)
	if err != nil || out["chosen"] != json.Number("9007199254740993") {
		t.Fatalf("out=%v err=%v", out, err)
	}
	state.Sys["zero"] = json.Number("NaN")
	if _, err := component.Invoke(canvas.WithState(context.Background(), state), nil); err == nil {
		t.Fatal("invalid number was treated as a truthy value or skipped")
	}
}

func TestNumericExcelJSONWritesNumericCells(t *testing.T) {
	params := numericJSON(t, `{"operation":"write","output_data":[[2,0.5,0.1,"9007199254740993"]]}`)
	component, err := NewExcelProcessorComponent(params)
	if err != nil {
		t.Fatal(err)
	}
	for _, inputs := range []map[string]any{nil, params} {
		out, err := component.Invoke(canvas.WithState(context.Background(), canvas.NewCanvasState("numeric", "numeric")), inputs)
		if err != nil {
			t.Fatal(err)
		}
		data, ok := out["bytes"].([]byte)
		if !ok || len(data) == 0 {
			t.Fatalf("missing workbook: %v", out)
		}
		archive, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
		if err != nil {
			t.Fatal(err)
		}
		found := false
		for _, entry := range archive.File {
			if entry.Name != "xl/worksheets/sheet1.xml" {
				continue
			}
			found = true
			r, err := entry.Open()
			if err != nil {
				t.Fatal(err)
			}
			var sheet struct {
				Cells []struct {
					Ref   string `xml:"r,attr"`
					Type  string `xml:"t,attr"`
					Value string `xml:"v"`
				} `xml:"sheetData>row>c"`
			}
			err = xml.NewDecoder(r).Decode(&sheet)
			r.Close()
			if err != nil || len(sheet.Cells) != 4 {
				t.Fatalf("sheet=%+v err=%v", sheet, err)
			}
			for i, want := range []string{"2", "0.5", "0.1"} {
				cell := sheet.Cells[i]
				if (cell.Type != "" && cell.Type != "n") || cell.Value != want {
					t.Fatalf("numeric cell %s serialized as %+v", want, cell)
				}
			}
			if cell := sheet.Cells[3]; cell.Type != "s" && cell.Type != "inlineStr" {
				t.Fatalf("explicit string identifier became numeric: %+v", cell)
			}
		}
		if !found {
			t.Fatal("workbook has no first worksheet")
		}
		workbook, err := excelize.OpenReader(bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		value, err := workbook.GetCellValue("Sheet1", "D1")
		workbook.Close()
		if err != nil || value != "9007199254740993" {
			t.Fatalf("identifier=%q err=%v", value, err)
		}
	}
}

func TestNumericExcelRejectsLossAndNonFinite(t *testing.T) {
	for _, number := range []string{"9007199254740993", "1.234567890123456789", "1e1000", "1e-1000"} {
		params := numericJSON(t, `{"operation":"write","output_data":[[`+number+`]]}`)
		component, err := NewExcelProcessorComponent(params)
		if err != nil {
			t.Fatal(err)
		}
		if out, err := component.Invoke(canvas.WithState(context.Background(), canvas.NewCanvasState("numeric", "numeric")), nil); err == nil || out != nil {
			t.Fatalf("accepted lossy spreadsheet number %s: out=%v err=%v", number, out, err)
		}
	}
	for _, value := range []any{json.Number("NaN"), math.Inf(-1)} {
		if _, err := excelNumericCell(value); err == nil {
			t.Fatalf("accepted nonfinite cell %v", value)
		}
	}
}

type numericCheckpoints struct {
	mu   sync.Mutex
	data map[string][]byte
}

func (s *numericCheckpoints) Get(_ context.Context, id string) ([]byte, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	value, ok := s.data[id]
	return append([]byte(nil), value...), ok, nil
}

func (s *numericCheckpoints) Set(_ context.Context, id string, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data[id] = append([]byte(nil), data...)
	return nil
}

func (s *numericCheckpoints) Delete(_ context.Context, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.data, id)
	return nil
}

func TestNumericCanvasCheckpointExactArithmetic(t *testing.T) {
	state := canvas.NewCanvasState("numeric-run", "numeric-task")
	state.Sys["counter"] = json.Number("9007199254740993")
	state.Sys["model_id"] = json.Number("9007199254740999")
	node, err := NewVariableAssignerComponent(numericJSON(t, `{"variables":[{"variable":"sys.counter","operator":"+=","parameter":2}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = node.Invoke(canvas.WithState(context.Background(), state), nil); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	resumed := canvas.NewCanvasState("new", "new")
	if err = json.Unmarshal(raw, resumed); err != nil {
		t.Fatal(err)
	}
	if resumed.Sys["counter"] != json.Number("9007199254740995") || resumed.Sys["model_id"] != json.Number("9007199254740999") {
		t.Fatalf("checkpoint rounded: %+v", resumed)
	}
	if _, err = node.Invoke(canvas.WithState(context.Background(), resumed), nil); err != nil {
		t.Fatal(err)
	}
	if resumed.Sys["counter"] != json.Number("9007199254740997") {
		t.Fatalf("counter=%#v", resumed.Sys["counter"])
	}
}
