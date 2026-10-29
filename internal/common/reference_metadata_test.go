package common

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestResolveReferenceMetadataPrecedenceAndSelection(t *testing.T) {
	wrap := func(preferences interface{}) map[string]interface{} {
		return map[string]interface{}{"reference_metadata": preferences}
	}
	tests := []struct {
		name            string
		request, config map[string]interface{}
		include         bool
		fields          []string
	}{
		{name: "missing"},
		{name: "empty", request: map[string]interface{}{}, config: map[string]interface{}{}},
		{name: "all fields", config: wrap(map[string]interface{}{"include": true}), include: true},
		{name: "explicit no fields", config: wrap(map[string]interface{}{"include": true, "fields": []interface{}{}}), include: true, fields: []string{}},
		{name: "typed no fields", request: wrap(map[string]interface{}{"fields": []string{}}), fields: []string{}},
		{name: "nil typed fields", request: wrap(map[string]interface{}{"fields": []string(nil)})},
		{name: "json null fields", request: wrap(map[string]interface{}{"fields": nil}), config: wrap(map[string]interface{}{"include": true, "fields": []string{"author"}}), include: true},
		{name: "null object inherits", request: wrap(nil), config: wrap(map[string]interface{}{"include": true, "fields": []string{"author"}}), include: true, fields: []string{"author"}},
		{name: "false override retains fields", request: wrap(map[string]interface{}{"include": false}), config: wrap(map[string]interface{}{"include": true, "fields": []interface{}{"author"}}), fields: []string{"author"}},
		{name: "fields override retains include", request: wrap(map[string]interface{}{"fields": []string{"date"}}), config: wrap(map[string]interface{}{"include": true, "fields": []interface{}{"author"}}), include: true, fields: []string{"date"}},
		{name: "empty fields override", request: wrap(map[string]interface{}{"fields": []interface{}{}}), config: wrap(map[string]interface{}{"include": true, "fields": []string{"author"}}), include: true, fields: []string{}},
		{name: "deduplicate typed fields", request: wrap(map[string]interface{}{"fields": []string{"author", "date", "author", ""}}), fields: []string{"author", "date", ""}},
		{name: "deduplicate json fields", request: wrap(map[string]interface{}{"fields": []interface{}{"author", "author"}}), fields: []string{"author"}},
		{name: "legacy ignored", request: map[string]interface{}{"include_metadata": true, "metadata_fields": []interface{}{"author"}}},
		{name: "legacy cannot override canonical", request: map[string]interface{}{"reference_metadata": map[string]interface{}{"include": false, "fields": []string{}}, "include_metadata": true, "metadata_fields": []interface{}{"author"}}, fields: []string{}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			include, fields, err := ResolveReferenceMetadata(test.request, test.config)
			if err != nil || include != test.include || !reflect.DeepEqual(fields, test.fields) {
				t.Fatalf("got (%v, %#v, %v), want (%v, %#v, nil)", include, fields, err, test.include, test.fields)
			}
		})
	}
}

func TestResolveReferenceMetadataRejectsMalformedSources(t *testing.T) {
	for _, invalid := range []interface{}{
		true,
		"all",
		[]interface{}{},
		map[string]interface{}{"extra": true},
		map[string]interface{}{"include": nil},
		map[string]interface{}{"include": "true"},
		map[string]interface{}{"include": 1},
		map[string]interface{}{"fields": "author"},
		map[string]interface{}{"fields": map[string]interface{}{}},
		map[string]interface{}{"fields": []interface{}{"author", nil}},
		map[string]interface{}{"fields": []interface{}{"author", 0}},
		map[string]interface{}{"fields": []interface{}{false}},
	} {
		payload := map[string]interface{}{"reference_metadata": invalid}
		validOverride := map[string]interface{}{"reference_metadata": map[string]interface{}{"include": true, "fields": []string{}}}
		for _, pair := range [][2]map[string]interface{}{{payload, nil}, {nil, payload}, {validOverride, payload}} {
			include, fields, err := ResolveReferenceMetadata(pair[0], pair[1])
			if err == nil || include || fields != nil {
				t.Fatalf("invalid preference %#v accepted: (%v, %#v, %v)", invalid, include, fields, err)
			}
		}
	}
}

func TestResolveReferenceMetadataDoesNotMutateInputs(t *testing.T) {
	for _, rawFields := range []interface{}{[]string{"author", "date", "author"}, []interface{}{"author", "date", "author"}} {
		request := map[string]interface{}{"reference_metadata": map[string]interface{}{"include": true, "fields": rawFields}}
		config := map[string]interface{}{"reference_metadata": map[string]interface{}{"include": false, "fields": []string{"config"}}}
		beforeRequest, _ := json.Marshal(request)
		beforeConfig, _ := json.Marshal(config)
		include, fields, err := ResolveReferenceMetadata(request, config)
		if err != nil || !include || !reflect.DeepEqual(fields, []string{"author", "date"}) {
			t.Fatalf("unexpected preference: (%v, %#v, %v)", include, fields, err)
		}
		fields[0] = "changed"
		afterRequest, _ := json.Marshal(request)
		afterConfig, _ := json.Marshal(config)
		if string(beforeRequest) != string(afterRequest) || string(beforeConfig) != string(afterConfig) {
			t.Fatal("resolver or returned field slice mutated caller payload")
		}
	}
}

func TestResolveReferenceMetadataJSONNullAndEmpty(t *testing.T) {
	for _, test := range []struct {
		body   string
		fields []string
	}{
		{body: `{"reference_metadata":{"include":true}}`},
		{body: `{"reference_metadata":{"include":true,"fields":null}}`},
		{body: `{"reference_metadata":{"include":true,"fields":[]}}`, fields: []string{}},
		{body: `{"reference_metadata":{"include":true,"fields":["author"]}}`, fields: []string{"author"}},
	} {
		var request map[string]interface{}
		if err := json.Unmarshal([]byte(test.body), &request); err != nil {
			t.Fatal(err)
		}
		include, fields, err := ResolveReferenceMetadata(request, nil)
		if err != nil || !include || !reflect.DeepEqual(fields, test.fields) {
			t.Fatalf("body %s produced (%v, %#v, %v)", test.body, include, fields, err)
		}
	}
}
