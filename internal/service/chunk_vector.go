package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"strconv"
	"strings"

	"gorm.io/gorm"

	"multirag/internal/dao"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
)

// ChunkVectorRequest identifies citation candidates in one dataset and its SQL
// documents. It is an internal service request, not a client-decoded capability.
type ChunkVectorRequest struct {
	DatasetID   string
	DocumentIDs []string
	ChunkIDs    []string
	Dimension   int
	// AllowTransportFallback permits zero vectors only for transport failures.
	// Authentication, invalid responses and cancelled contexts still fail.
	AllowTransportFallback bool
}

// ChunkVectorFallback identifies a zero vector that was not read from storage.
type ChunkVectorFallback string

const (
	ChunkVectorMissing       ChunkVectorFallback = "missing_chunk"
	ChunkVectorMissingField  ChunkVectorFallback = "missing_vector"
	ChunkVectorTransportFail ChunkVectorFallback = "transport_failure"
)

// ChunkVectorResult distinguishes hydrated values (including actual zero
// vectors) from citation fallback values. Every vector is independently owned.
type ChunkVectorResult struct {
	Vectors  map[string][]float64
	Fallback map[string]ChunkVectorFallback
}

var ErrChunkVectorScope = errors.New("invalid citation vector scope")
var ErrChunkVectorData = errors.New("invalid citation vector data")

type vectorFetcher interface {
	Search(context.Context, *types.SearchRequest) (*types.SearchResult, error)
}

type chunkVectorScope struct {
	datasetID, tenantID string
	documentIDs         []string
	chunkIDs            []string
}

// FetchChunkVectors authorizes and hydrates one citation candidate selection.
// It does not implement the Go ask or citation insertion execution path.
func (s *ChunkService) FetchChunkVectors(ctx context.Context, req *ChunkVectorRequest, userID string) (*ChunkVectorResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if req == nil || !exactVectorID(req.DatasetID) || !exactVectorID(userID) || len(req.DocumentIDs) > 10000 || len(req.ChunkIDs) > 10000 {
		return nil, ErrChunkVectorScope
	}
	docIDs, err := vectorIDs(req.DocumentIDs)
	if err != nil || len(docIDs) == 0 {
		return nil, ErrChunkVectorScope
	}
	chunkIDs, err := vectorIDs(req.ChunkIDs)
	if err != nil || !validVectorAllocation(len(chunkIDs), req.Dimension) {
		return nil, ErrChunkVectorScope
	}
	if s == nil || s.kbDAO == nil || dao.DB == nil {
		return nil, fmt.Errorf("citation vector authorization is unavailable")
	}
	kb, err := s.kbDAO.GetReadableByID(ctx, req.DatasetID, userID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrDatasetAccessDenied
	}
	if err != nil {
		return nil, fmt.Errorf("authorize citation dataset: %w", err)
	}
	if kb == nil || kb.ID != req.DatasetID || !exactVectorID(kb.TenantID) {
		return nil, ErrDatasetAccessDenied
	}
	if _, err := searchbotTenant(ctx, kb.TenantID); err != nil {
		return nil, fmt.Errorf("authorize citation tenant: %w", err)
	}
	var documents []entity.Document
	if err := dao.DB.WithContext(ctx).Select("id", "kb_id").Where("id IN ? AND kb_id = ? AND status = ?", docIDs, kb.ID, string(entity.StatusValid)).Find(&documents).Error; err != nil {
		return nil, fmt.Errorf("authorize citation documents: %w", err)
	}
	if len(documents) != len(docIDs) {
		return nil, ErrDatasetAccessDenied
	}
	documentsByID := make(map[string]bool, len(documents))
	for _, document := range documents {
		if document.KbID != kb.ID || documentsByID[document.ID] {
			return nil, ErrDatasetAccessDenied
		}
		documentsByID[document.ID] = true
	}
	for _, id := range docIDs {
		if !documentsByID[id] {
			return nil, ErrDatasetAccessDenied
		}
	}
	return fetchChunkVectors(ctx, s.docEngine, chunkVectorScope{datasetID: kb.ID, tenantID: kb.TenantID, documentIDs: docIDs, chunkIDs: chunkIDs}, req.Dimension, req.AllowTransportFallback)
}

// fetchChunkVectors accepts only a scope that its caller has already authorized.
// A single projected read avoids tenant scans, per-chunk IO and vector transfer
// in the main retrieval response. Infinity uses this same read when no hydrated
// vectors were supplied; skipping it would falsely label zeroes as hydrated.
func fetchChunkVectors(ctx context.Context, store vectorFetcher, scope chunkVectorScope, dim int, allowTransportFallback bool) (*ChunkVectorResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if !exactVectorID(scope.datasetID) || !exactVectorID(scope.tenantID) || !validVectorAllocation(len(scope.chunkIDs), dim) || len(scope.documentIDs) == 0 || len(scope.documentIDs) > 10000 {
		return nil, ErrChunkVectorScope
	}
	docs, err := vectorIDs(scope.documentIDs)
	if err != nil {
		return nil, ErrChunkVectorScope
	}
	ids, err := vectorIDs(scope.chunkIDs)
	if err != nil {
		return nil, ErrChunkVectorScope
	}
	result := &ChunkVectorResult{Vectors: make(map[string][]float64, len(ids)), Fallback: make(map[string]ChunkVectorFallback)}
	if len(ids) == 0 {
		return result, nil
	}
	if store == nil {
		return nil, fmt.Errorf("citation vector engine is unavailable")
	}
	field := fmt.Sprintf("q_%d_vec", dim)
	response, err := store.Search(ctx, &types.SearchRequest{
		IndexNames: []string{"multirag_" + scope.tenantID}, KbIDs: []string{scope.datasetID},
		SelectFields: []string{"id", "kb_id", "doc_id", field}, Limit: len(ids),
		Filter: map[string]interface{}{"id": ids, "doc_id": docs, "available_int": 1},
	})
	// A store can return nil error after its context was cancelled. Never convert
	// cancellation into apparently successful citation fallback.
	if ctxErr := ctx.Err(); ctxErr != nil {
		return nil, ctxErr
	}
	if err != nil {
		var transport net.Error
		if !allowTransportFallback || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) || errors.Is(err, ErrDatasetAccessDenied) || !errors.As(err, &transport) {
			return nil, fmt.Errorf("fetch citation vectors: %w", err)
		}
		for _, id := range ids {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			result.Vectors[id] = make([]float64, dim)
			result.Fallback[id] = ChunkVectorTransportFail
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		return result, nil
	}
	if response == nil {
		return nil, fmt.Errorf("%w: missing engine response", ErrChunkVectorData)
	}
	if response.Total != int64(len(response.Chunks)) || response.Total > int64(len(ids)) {
		return nil, fmt.Errorf("%w: engine returned an inconsistent candidate count", ErrChunkVectorData)
	}
	allowedIDs := make(map[string]bool, len(ids))
	allowedDocs := make(map[string]bool, len(docs))
	for _, id := range ids {
		allowedIDs[id] = true
	}
	for _, id := range docs {
		allowedDocs[id] = true
	}
	for _, chunk := range response.Chunks {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		id, idOK := chunk["id"].(string)
		docID, docOK := chunk["doc_id"].(string)
		if !idOK || !allowedIDs[id] || !docOK || !allowedDocs[docID] || referenceKBID(chunk["kb_id"]) != scope.datasetID {
			return nil, fmt.Errorf("%w: returned candidate is outside authorized scope", ErrChunkVectorScope)
		}
		if _, duplicate := result.Vectors[id]; duplicate {
			return nil, fmt.Errorf("%w: duplicate candidate", ErrChunkVectorData)
		}
		raw, exists := chunk[field]
		if !exists {
			result.Vectors[id] = make([]float64, dim)
			result.Fallback[id] = ChunkVectorMissingField
			continue
		}
		vector, err := parseChunkVector(raw, dim)
		if err != nil {
			return nil, err
		}
		result.Vectors[id] = vector
	}
	for _, id := range ids {
		if _, exists := result.Vectors[id]; !exists {
			result.Vectors[id] = make([]float64, dim)
			result.Fallback[id] = ChunkVectorMissing
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

func exactVectorID(id string) bool { return id != "" && id == strings.TrimSpace(id) }

func vectorIDs(ids []string) ([]string, error) {
	result := make([]string, 0, len(ids))
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		if !exactVectorID(id) {
			return nil, ErrChunkVectorScope
		}
		if !seen[id] {
			seen[id] = true
			result = append(result, id)
		}
	}
	return result, nil
}

func validVectorAllocation(count, dim int) bool {
	// Bound the single engine request and the total result allocation (128 MiB).
	return dim > 0 && dim <= 65536 && count <= 10000 && count <= (16*1024*1024)/dim
}

func parseChunkVector(raw interface{}, dim int) ([]float64, error) {
	vector := make([]float64, dim)
	switch value := raw.(type) {
	case string:
		parts := strings.Split(value, "\t")
		if len(parts) != dim {
			return nil, fmt.Errorf("%w: vector dimension mismatch", ErrChunkVectorData)
		}
		for i, part := range parts {
			number, err := strconv.ParseFloat(strings.TrimSpace(part), 64)
			if err != nil {
				return nil, fmt.Errorf("%w: nonnumeric vector value", ErrChunkVectorData)
			}
			vector[i] = number
		}
	case []float64:
		if len(value) != dim {
			return nil, fmt.Errorf("%w: vector dimension mismatch", ErrChunkVectorData)
		}
		copy(vector, value)
	case []float32:
		if len(value) != dim {
			return nil, fmt.Errorf("%w: vector dimension mismatch", ErrChunkVectorData)
		}
		for i, number := range value {
			vector[i] = float64(number)
		}
	case []interface{}:
		if len(value) != dim {
			return nil, fmt.Errorf("%w: vector dimension mismatch", ErrChunkVectorData)
		}
		for i, value := range value {
			var err error
			switch value := value.(type) {
			case float64:
				vector[i] = value
			case float32:
				vector[i] = float64(value)
			case json.Number:
				vector[i], err = value.Float64()
			case string:
				vector[i], err = strconv.ParseFloat(value, 64)
			default:
				return nil, fmt.Errorf("%w: nonnumeric vector value", ErrChunkVectorData)
			}
			if err != nil {
				return nil, fmt.Errorf("%w: invalid vector value", ErrChunkVectorData)
			}
		}
	default:
		return nil, fmt.Errorf("%w: vector is not an array or tab-separated string", ErrChunkVectorData)
	}
	for _, number := range vector {
		if math.IsNaN(number) || math.IsInf(number, 0) {
			return nil, fmt.Errorf("%w: nonfinite vector value", ErrChunkVectorData)
		}
	}
	return vector, nil
}
