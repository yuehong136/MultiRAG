package common

import "fmt"

// ResolveReferenceMetadata applies canonical reference_metadata preferences from
// config, then request. A nil fields slice selects all fields; a non-nil empty
// slice selects none. Each source is validated before overrides are applied.
func ResolveReferenceMetadata(requestPayload, configPayload map[string]interface{}) (bool, []string, error) {
	include := false
	var fields []string
	for _, payload := range []map[string]interface{}{configPayload, requestPayload} {
		raw := payload["reference_metadata"]
		if raw == nil {
			continue
		}
		preferences, ok := raw.(map[string]interface{})
		if !ok {
			return false, nil, fmt.Errorf("reference_metadata must be an object")
		}
		for key := range preferences {
			if key != "include" && key != "fields" {
				return false, nil, fmt.Errorf("reference_metadata contains an unknown property")
			}
		}
		if rawInclude, exists := preferences["include"]; exists {
			value, ok := rawInclude.(bool)
			if !ok {
				return false, nil, fmt.Errorf("reference_metadata.include must be a boolean")
			}
			include = value
		}
		if rawFields, exists := preferences["fields"]; exists {
			var err error
			fields, err = referenceMetadataFields(rawFields)
			if err != nil {
				return false, nil, err
			}
		}
	}
	return include, fields, nil
}

func referenceMetadataFields(raw interface{}) ([]string, error) {
	if raw == nil {
		return nil, nil
	}
	var values []string
	switch typed := raw.(type) {
	case []string:
		if typed == nil {
			return nil, nil
		}
		values = typed
	case []interface{}:
		if typed == nil {
			return nil, nil
		}
		values = make([]string, 0, len(typed))
		for _, item := range typed {
			value, ok := item.(string)
			if !ok {
				return nil, fmt.Errorf("reference_metadata.fields must contain only strings")
			}
			values = append(values, value)
		}
	default:
		return nil, fmt.Errorf("reference_metadata.fields must be an array or null")
	}
	fields := make([]string, 0, len(values))
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if _, exists := seen[value]; !exists {
			fields = append(fields, value)
			seen[value] = struct{}{}
		}
	}
	return fields, nil
}
