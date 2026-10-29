package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"

	"multirag/internal/dao"
	"multirag/internal/entity"
)

var ErrRetrievalArgument = errors.New("invalid retrieval request")

// savedRetrievalRequest overlays only present saved fields. Decoding into the
// request's concrete types preserves false, zero, empty arrays and numeric IDs.
func savedRetrievalRequest(req *RetrievalTestRequest, config map[string]interface{}) (*RetrievalTestRequest, error) {
	result := *req
	fields := map[string]interface{}{}
	for _, key := range []string{"similarity_threshold", "vector_similarity_weight", "top_k", "use_kg", "cross_languages", "rerank_id", "tenant_rerank_id", "keyword", "doc_ids", "meta_data_filter"} {
		if value, ok := config[key]; ok {
			if value == nil && (key == "similarity_threshold" || key == "vector_similarity_weight" || key == "top_k" || key == "use_kg" || key == "keyword") {
				return nil, fmt.Errorf("%w: saved %s cannot be null", ErrRetrievalArgument, key)
			}
			fields[key] = value
		}
	}
	raw, err := json.Marshal(fields)
	if err != nil {
		return nil, fmt.Errorf("%w: invalid saved search configuration", ErrRetrievalArgument)
	}
	var saved RetrievalTestRequest
	if err := json.Unmarshal(raw, &saved); err != nil {
		return nil, fmt.Errorf("%w: invalid saved search configuration", ErrRetrievalArgument)
	}
	for key := range fields {
		switch key {
		case "similarity_threshold":
			result.SimilarityThreshold = saved.SimilarityThreshold
		case "vector_similarity_weight":
			result.VectorSimilarityWeight = saved.VectorSimilarityWeight
		case "top_k":
			result.TopK = saved.TopK
		case "use_kg":
			result.UseKG = saved.UseKG
		case "cross_languages":
			result.CrossLanguages = saved.CrossLanguages
		case "rerank_id":
			result.RerankID = saved.RerankID
		case "tenant_rerank_id":
			result.TenantRerankID = saved.TenantRerankID
		case "keyword":
			result.Keyword = saved.Keyword
		case "doc_ids":
			result.DocIDs = saved.DocIDs
		case "meta_data_filter":
			result.Filter = saved.Filter
		}
	}
	if err := validateRetrievalRequest(&result); err != nil {
		return nil, err
	}
	return &result, nil
}

// readableRetrievalConfig re-reads authorized settings without JSONMap's float64
// conversion, which would round numeric model IDs and metadata operands.
func readableRetrievalConfig(ctx context.Context, userID, searchID string) (*entity.Search, map[string]interface{}, error) {
	saved, err := readableSearch(ctx, userID, searchID)
	if err != nil {
		return nil, nil, err
	}
	var raw []byte
	if err := dao.DB.WithContext(ctx).Model(&entity.Search{}).Select("search_config").Where("id = ? AND tenant_id = ? AND status = ?", saved.ID, saved.TenantID, "1").Row().Scan(&raw); err != nil {
		return nil, nil, err
	}
	var config map[string]interface{}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&config); err != nil || config == nil {
		return nil, nil, fmt.Errorf("%w: invalid saved search configuration", ErrRetrievalArgument)
	}
	return saved, config, nil
}

func validateRetrievalRequest(req *RetrievalTestRequest) error {
	invalid := func(message string) error { return fmt.Errorf("%w: %s", ErrRetrievalArgument, message) }
	if req == nil || strings.TrimSpace(req.Question) == "" {
		return invalid("question is required")
	}
	if len(req.DatasetIDs) == 0 {
		return invalid("dataset_ids must be a non-empty array")
	}
	if req.Page != nil && *req.Page < 1 || req.Size != nil && *req.Size < 1 {
		return invalid("page and size must be positive")
	}
	page, size := 1, 30
	if req.Page != nil {
		page = *req.Page
	}
	if req.Size != nil {
		size = *req.Size
	}
	if page-1 > int(^uint(0)>>1)/size {
		return invalid("page offset is too large")
	}
	if req.TopK != nil && (*req.TopK < 0 || *req.TopK > 2048) {
		return invalid("top_k must be between 0 and 2048")
	}
	if req.TenantRerankID != nil && *req.TenantRerankID <= 0 {
		return invalid("tenant_rerank_id must be positive")
	}
	if req.TenantRerankID != nil && req.RerankID != nil && *req.RerankID != "" {
		return invalid("select one rerank model")
	}
	for _, value := range []*float64{req.SimilarityThreshold, req.VectorSimilarityWeight} {
		if value != nil && (math.IsNaN(*value) || math.IsInf(*value, 0) || *value < 0 || *value > 1) {
			return invalid("similarity values must be finite numbers in [0, 1]")
		}
	}
	for _, language := range req.CrossLanguages {
		if strings.TrimSpace(language) == "" {
			return invalid("cross_languages must contain non-empty strings")
		}
	}
	return nil
}

// ResolveRetrievalModelSelector validates a numeric active tenant model binding
// and returns the canonical provider selector consumed by model services.
func ResolveRetrievalModelSelector(ctx context.Context, tenantID string, modelType entity.ModelType, numericID *int64, selector string) (string, error) {
	return retrievalModelSelector(ctx, tenantID, modelType, numericID, selector)
}

// UnmarshalJSON keeps exact metadata numbers on the HTTP and saved-config paths.
func (r *RetrievalTestRequest) UnmarshalJSON(raw []byte) error {
	type requestJSON RetrievalTestRequest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	decoder.DisallowUnknownFields()
	return decoder.Decode((*requestJSON)(r))
}

// ValidateDatasetSearchRequest validates parameters before database work.
func ValidateDatasetSearchRequest(req *RetrievalTestRequest) error {
	return validateRetrievalRequest(req)
}
