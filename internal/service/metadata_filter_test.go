package service

import (
	"context"
	"errors"
	"math"
	"multirag/internal/common"
	"multirag/internal/entity/models"
	"reflect"
	"testing"
)

func metadataFixture() common.MetaData {
	return common.MetaData{
		"status":  {"Ready": {"new", "old"}, "Draft": {"draft"}},
		"version": {"v1": {"old"}, "v2": {"new", "draft"}},
		"number":  {"0": {"zero"}, "10": {"ten"}},
		"boolean": {"false": {"false"}, "true": {"true"}},
	}
}

func manualMetadataConfig(conditions []MetaFilterCondition, logic string) map[string]interface{} {
	items := make([]interface{}, 0, len(conditions))
	for _, condition := range conditions {
		items = append(items, map[string]interface{}{"key": condition.Key, "op": condition.Op, "value": condition.Value})
	}
	return map[string]interface{}{"method": "manual", "manual": items, "logic": logic}
}

func TestApplyMetaFilterUsesCommonMatcherWithActualDocumentSlices(t *testing.T) {
	cases := []struct {
		condition MetaFilterCondition
		want      []string
	}{
		{MetaFilterCondition{Key: "status", Op: "contains", Value: "READY"}, []string{"new", "old"}},
		{MetaFilterCondition{Key: "status", Op: "not contains", Value: "READY"}, []string{"draft"}},
		{MetaFilterCondition{Key: "status", Op: "not in", Value: "READY"}, []string{"draft"}},
		{MetaFilterCondition{Key: "number", Op: ">", Value: 2}, []string{"ten"}},
		{MetaFilterCondition{Key: "number", Op: "is", Value: 0}, []string{"zero"}},
		{MetaFilterCondition{Key: "boolean", Op: "is", Value: false}, []string{"false"}},
		{MetaFilterCondition{Key: "number", Op: "in", Value: []interface{}{0, 10.0}}, []string{"ten", "zero"}},
		{MetaFilterCondition{Key: "status", Op: "in", Value: []string{"Ready"}}, []string{"new", "old"}},
	}
	for _, test := range cases {
		if got := ApplyMetaFilter(metadataFixture(), []MetaFilterCondition{test.condition}, "and"); !reflect.DeepEqual(got, test.want) {
			t.Fatalf("%s %v returned %v, want %v", test.condition.Op, test.condition.Value, got, test.want)
		}
		filter := manualMetadataConfig([]MetaFilterCondition{test.condition}, "and")
		got, empty := ApplyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", nil, nil)
		if !reflect.DeepEqual(got, test.want) || empty {
			t.Fatalf("manual %s %v returned %v/%v, want %v/false", test.condition.Op, test.condition.Value, got, empty, test.want)
		}
	}
}

func TestMetadataFilterIntersectsBaseInEveryMode(t *testing.T) {
	conditions := []MetaFilterCondition{{Key: "version", Op: "is", Value: "v2"}}
	for _, method := range []string{"manual", "auto", "semi_auto"} {
		for _, test := range []struct {
			base  []string
			want  []string
			empty bool
		}{
			{nil, []string{"draft", "new"}, false},
			{[]string{}, []string{}, true},
			{[]string{"old", "new", "outside", "new"}, []string{"new"}, false},
			{[]string{"old"}, []string{"-999"}, true},
		} {
			config := manualMetadataConfig(conditions, "and")
			config["method"] = method
			config["semi_auto"] = []interface{}{"version"}
			calls := 0
			generator := func(_ common.MetaData, _ string, _ map[string]string) (*MetaFilterResult, error) {
				calls++
				return &MetaFilterResult{Conditions: conditions, Logic: "and"}, nil
			}
			got, empty := applyMetaDataFilter(context.Background(), config, metadataFixture(), "question", test.base, nil, generator)
			if !reflect.DeepEqual(got, test.want) || empty != test.empty {
				t.Fatalf("%s base %v returned %v/%v, want %v/%v", method, test.base, got, empty, test.want, test.empty)
			}
			if (method == "manual" || (test.base != nil && len(test.base) == 0)) && calls != 0 {
				t.Fatalf("%s base %v unexpectedly called generator", method, test.base)
			}
		}
	}
}

func TestMetadataNoFilterPreservesNilAndEmptySelections(t *testing.T) {
	for _, base := range [][]string{nil, {}, {"new", "old", "new"}} {
		for _, filter := range []map[string]interface{}{nil, {}, {"method": "unknown"}, manualMetadataConfig(nil, "and")} {
			generator := func(_ common.MetaData, _ string, _ map[string]string) (*MetaFilterResult, error) {
				t.Fatal("no filter invoked a model")
				return nil, nil
			}
			got, empty := applyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", base, nil, generator)
			if !reflect.DeepEqual(got, base) || empty != (base != nil && len(base) == 0) {
				t.Fatalf("filter %v base %#v became %#v/%v", filter, base, got, empty)
			}
		}
	}
}

func TestInvalidMetadataOperandsFailClosedBeforeBooleanEvaluation(t *testing.T) {
	for _, operand := range []interface{}{nil, false, 0, map[string]interface{}{}, []interface{}{nil}, []interface{}{[]string{"ready"}}, []interface{}{math.Inf(1)}, []interface{}{"Ready", nil}} {
		for _, logic := range []string{"and", "or"} {
			conditions := []MetaFilterCondition{{Key: "status", Op: "is", Value: "Ready"}, {Key: "status", Op: "not in", Value: operand}}
			filter := manualMetadataConfig(conditions, logic)
			if ValidateMetaDataFilter(filter) == nil {
				t.Fatalf("invalid operand %T accepted", operand)
			}
			got, empty := ApplyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", nil, []string{"new", "old"})
			if !reflect.DeepEqual(got, []string{"-999"}) || !empty {
				t.Fatalf("invalid %T/%s broadened scope to %v/%v", operand, logic, got, empty)
			}
		}
	}
}

func TestSemiAutoRejectsInvalidSelectionsWithoutModelCalls(t *testing.T) {
	selections := []interface{}{nil, []interface{}{}, []interface{}{"missing"}, []interface{}{"version", "missing"}, []interface{}{""}, []interface{}{false}, []interface{}{map[string]interface{}{"key": "version", "op": 0}}, []interface{}{map[string]interface{}{"key": "version", "op": "unsupported"}}}
	for _, selection := range selections {
		calls := 0
		generator := func(_ common.MetaData, _ string, _ map[string]string) (*MetaFilterResult, error) {
			calls++
			return &MetaFilterResult{Conditions: []MetaFilterCondition{{Key: "version", Op: "=", Value: "v2"}}}, nil
		}
		got, empty := applyMetaDataFilter(context.Background(), map[string]interface{}{"method": "semi_auto", "semi_auto": selection}, metadataFixture(), "question", nil, nil, generator)
		if !reflect.DeepEqual(got, []string{"-999"}) || !empty || calls != 0 {
			t.Fatalf("invalid selection %#v returned %v/%v with %d model calls", selection, got, empty, calls)
		}
	}
}

func TestSemiAutoGeneratedPredicatesStayWithinSelectedFields(t *testing.T) {
	for _, conditions := range [][]MetaFilterCondition{nil, {{Key: "status", Op: "is", Value: "Ready"}}} {
		generator := func(metadata common.MetaData, _ string, constraints map[string]string) (*MetaFilterResult, error) {
			if len(metadata) != 1 || metadata["version"] == nil || constraints["version"] != "=" {
				t.Fatalf("generator received unselected fields or lost constraints: %v/%v", metadata, constraints)
			}
			return &MetaFilterResult{Conditions: conditions, Logic: "or"}, nil
		}
		filter := map[string]interface{}{"method": "semi_auto", "semi_auto": []interface{}{map[string]interface{}{"key": "version", "op": "is"}}}
		got, empty := applyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", []string{"new"}, nil, generator)
		if !reflect.DeepEqual(got, []string{"-999"}) || !empty {
			t.Fatalf("unselected generated predicate returned %v/%v", got, empty)
		}
	}
}

func TestSemiAutoGeneratedOperatorsRespectSelectedConstraints(t *testing.T) {
	for _, test := range []struct {
		name       string
		constraint string
		generated  string
		want       []string
		empty      bool
	}{
		{"same field wrong operator", "is", "not is", []string{"-999"}, true},
		{"constraint alias", "is", "=", []string{"new"}, false},
		{"generated alias", "=", "is", []string{"new"}, false},
		{"empty constraint permits operator", "", "not is", []string{"old"}, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			calls := 0
			generator := func(_ common.MetaData, _ string, constraints map[string]string) (*MetaFilterResult, error) {
				calls++
				wantConstraint := test.constraint
				if wantConstraint == "is" {
					wantConstraint = "="
				}
				if constraints["version"] != wantConstraint {
					t.Fatalf("normalized constraint = %q, want %q", constraints["version"], wantConstraint)
				}
				return &MetaFilterResult{Conditions: []MetaFilterCondition{{Key: "version", Op: test.generated, Value: "v2"}}, Logic: "or"}, nil
			}
			filter := map[string]interface{}{"method": "semi_auto", "semi_auto": []interface{}{map[string]interface{}{"key": "version", "op": test.constraint}}}
			got, empty := applyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", []string{"old", "new"}, nil, generator)
			if !reflect.DeepEqual(got, test.want) || empty != test.empty || calls != 1 {
				t.Fatalf("generated %q with constraint %q returned %v/%v with %d model calls, want %v/%v", test.generated, test.constraint, got, empty, calls, test.want, test.empty)
			}
		})
	}
}

func TestMetadataFailuresAndCancellationCannotBecomeUnfilteredRetrieval(t *testing.T) {
	filter := map[string]interface{}{"method": "auto"}
	for _, generator := range []metaFilterGenerator{
		func(_ common.MetaData, _ string, _ map[string]string) (*MetaFilterResult, error) {
			return nil, errors.New("failed")
		},
		func(_ common.MetaData, _ string, _ map[string]string) (*MetaFilterResult, error) {
			return nil, nil
		},
	} {
		got, empty := applyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", []string{"new"}, nil, generator)
		if !reflect.DeepEqual(got, []string{"-999"}) || !empty {
			t.Fatalf("failed generation returned %v/%v", got, empty)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, base := range [][]string{nil, {}, {"new"}} {
		generator := func(_ common.MetaData, _ string, _ map[string]string) (*MetaFilterResult, error) {
			t.Fatal("canceled request invoked a model")
			return nil, nil
		}
		_, empty := applyMetaDataFilter(ctx, filter, metadataFixture(), "question", base, nil, generator)
		if !empty {
			t.Fatal("canceled request was not an authoritative empty result")
		}
	}
}

func TestAutoWithoutGeneratedPredicatesPreservesFallback(t *testing.T) {
	for _, base := range [][]string{nil, {"new", "without-metadata"}} {
		generator := func(_ common.MetaData, _ string, _ map[string]string) (*MetaFilterResult, error) {
			return &MetaFilterResult{Conditions: []MetaFilterCondition{}}, nil
		}
		got, empty := applyMetaDataFilter(context.Background(), map[string]interface{}{"method": "auto"}, metadataFixture(), "question", base, nil, generator)
		if !reflect.DeepEqual(got, base) || empty {
			t.Fatalf("auto empty predicate fallback = %#v/%v, want %#v/false", got, empty, base)
		}
	}
}

func TestEmptyBaseDoesNotInvokeManualResolver(t *testing.T) {
	filter := manualMetadataConfig([]MetaFilterCondition{{Key: "version", Op: "is", Value: "v2"}}, "and")
	resolver := func(condition map[string]interface{}) map[string]interface{} {
		t.Fatal("empty base invoked resolver")
		return condition
	}
	got, empty := ApplyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", nil, []string{}, resolver)
	if got == nil || len(got) != 0 || !empty {
		t.Fatalf("empty base became %v/%v", got, empty)
	}
}

func TestMetadataResolverCannotModifySavedFilter(t *testing.T) {
	filter := manualMetadataConfig([]MetaFilterCondition{{Key: "version", Op: "in", Value: []interface{}{"v1"}}}, "and")
	resolver := func(condition map[string]interface{}) map[string]interface{} {
		condition["value"].([]interface{})[0] = "v2"
		return condition
	}
	got, empty := ApplyMetaDataFilter(context.Background(), filter, metadataFixture(), "question", nil, []string{"old", "new"}, resolver)
	if !reflect.DeepEqual(got, []string{"new"}) || empty {
		t.Fatalf("resolved scope = %v/%v", got, empty)
	}
	value := filter["manual"].([]interface{})[0].(map[string]interface{})["value"].([]interface{})[0]
	if value != "v1" {
		t.Fatalf("saved operand changed to %v", value)
	}
}

func TestMetadataCheckedPropagatesModelFailureAndCancellation(t *testing.T) {
	filter := map[string]interface{}{"method": "auto"}
	if _, _, err := ApplyMetaDataFilterChecked(context.Background(), filter, metadataFixture(), "q", nil, nil); err == nil {
		t.Fatal("model failure became an empty success")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, _, err := ApplyMetaDataFilterChecked(ctx, filter, metadataFixture(), "q", nil, nil); !errors.Is(err, context.Canceled) {
		t.Fatal("cancellation became an empty success")
	}
	service := NewMetadataService()
	if _, err := service.GetFlattedMetaByKBsContext(ctx, []string{"forbidden"}); !errors.Is(err, context.Canceled) {
		t.Fatal("canceled metadata request reached SQL")
	}
}

func TestMetadataGeneratedJSONRequiresConditionsArray(t *testing.T) {
	for _, payload := range []string{"null", "{}", `{"conditions":null}`, `{"conditions":{}}`, `{"conditions":[],"logic":null}`, `{"conditions":[null]}`, `{"conditions":[{"key":"status","op":"not in","value":false}]}`, `{"conditions":[]}`} {
		calls := 0
		answer := payload
		name := "controlled"
		driver := &searchbotTestDriver{chat: func(_ string, _ *models.APIConfig, _ []models.Message, _ *models.ChatConfig) (*models.ChatResponse, error) {
			calls++
			return &models.ChatResponse{Answer: &answer}, nil
		}}
		model := models.NewChatModel(driver, &name, nil)
		ids, empty, err := ApplyMetaDataFilterChecked(context.Background(), map[string]interface{}{"method": "auto"}, metadataFixture(), "q", model, nil)
		if calls != 1 {
			t.Fatal("generated JSON test did not reach model")
		}
		if payload == `{"conditions":[]}` {
			if err != nil || empty || ids != nil {
				t.Fatal("explicit empty generation must retain optional-filter fallback")
			}
		} else if err == nil {
			t.Fatalf("invalid model shape became unfiltered retrieval: %s", payload)
		}
	}
}
