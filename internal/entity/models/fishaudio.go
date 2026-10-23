package models

import (
	"fmt"
	"math"
	"net/http"
	"strings"
)

// FishAudioModel supports speech synthesis, transcription, discovery and credit balance.
type FishAudioModel struct {
	*DummyModel
	driver *openAIChatDriver
}

func NewFishAudioModel(baseURL map[string]string, suffix URLSuffix) *FishAudioModel {
	driver := newOpenAIChatDriver(baseURL, suffix, "fishaudio", nil)
	return &FishAudioModel{DummyModel: driver.DummyModel, driver: driver}
}

func (f *FishAudioModel) Name() string { return "fishaudio" }

func (f *FishAudioModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := f.driver.request(http.MethodGet, f.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Items *[]struct {
			ID    string `json:"_id"`
			Title string `json:"title"`
		} `json:"items"`
		Error any `json:"error"`
	}
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, err
	}
	if result.Items == nil || result.Error != nil {
		return nil, fmt.Errorf("fishaudio: missing model list or provider error")
	}
	names := make([]string, 0, len(*result.Items))
	for _, item := range *result.Items {
		name := strings.TrimSpace(item.Title)
		if name == "" {
			name = strings.TrimSpace(item.ID)
		}
		if name == "" {
			return nil, fmt.Errorf("fishaudio: model has no title or ID")
		}
		names = append(names, name)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (f *FishAudioModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := f.driver.request(http.MethodGet, f.URLSuffix.Balance, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result map[string]interface{}
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, err
	}
	credit, ok := result["credit"].(float64)
	if !ok || math.IsNaN(credit) || math.IsInf(credit, 0) || result["error"] != nil {
		return nil, fmt.Errorf("fishaudio: missing or invalid credit balance")
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}

func (f *FishAudioModel) CheckConnection(api *APIConfig) error {
	_, err := f.ListModels(api)
	return err
}

var _ ModelDriver = (*FishAudioModel)(nil)
