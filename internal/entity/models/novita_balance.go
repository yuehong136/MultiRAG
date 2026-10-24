package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
)

func (n *NovitaModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := n.request(http.MethodGet, n.URLSuffix.Balance, local, nil)
	if err != nil {
		if err := requestContext(local).Err(); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("novita: balance request failed")
	}
	defer resp.Body.Close()
	var response struct {
		AvailableBalance *string         `json:"availableBalance"`
		Error            json.RawMessage `json:"error"`
		Code             json.RawMessage `json:"code"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &response); err != nil || response.AvailableBalance == nil || len(response.Error) > 0 && string(response.Error) != "null" {
		return nil, fmt.Errorf("novita: invalid balance response")
	}
	if err := validateModelResponseCode(response.Code); err != nil {
		return nil, fmt.Errorf("novita: balance provider error")
	}
	raw := *response.AvailableBalance
	// Native monetary strings are integers in 1/10000 USD, not USD floats.
	digits := strings.TrimPrefix(raw, "-")
	if digits == "" {
		return nil, fmt.Errorf("novita: invalid balance amount")
	}
	for _, ch := range digits {
		if ch < '0' || ch > '9' {
			return nil, fmt.Errorf("novita: invalid balance amount")
		}
	}
	units, err := strconv.ParseFloat(raw, 64)
	if err != nil || math.IsNaN(units) || math.IsInf(units, 0) {
		return nil, fmt.Errorf("novita: invalid balance amount")
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return map[string]interface{}{"balance": units / 10000, "currency": "USD"}, nil
}
