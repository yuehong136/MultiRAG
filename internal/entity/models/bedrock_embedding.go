package models

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"unicode/utf8"
)

// Embed uses InvokeModel; Converse has no embedding operation. Resolve signing
// credentials once for the whole call, including Titan's single-input requests.
func (b *BedrockModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" || strings.TrimSpace(*name) != *name {
		return nil, fmt.Errorf("bedrock: embedding model id is required")
	}
	modelID := *name
	titan, batchSize, maxChars := false, 96, 0
	switch modelID {
	case "amazon.titan-embed-text-v1", "amazon.titan-embed-text-v2:0":
		titan, batchSize, maxChars = true, 1, 50000
	case "cohere.embed-english-v3", "cohere.embed-multilingual-v3":
		maxChars = 2048
	case "cohere.embed-v4:0":
		// The provider enforces its 128k token limit; bound local payloads too.
		maxChars = MaxMediaBytes / 4
	default:
		return nil, fmt.Errorf("bedrock: unsupported embedding model")
	}
	dimension := 0
	if config != nil {
		dimension = config.Dimension
	}
	if !bedrockEmbeddingDimension(modelID, dimension) {
		return nil, fmt.Errorf("bedrock: unsupported embedding dimension")
	}
	// Leave bounded space for field names/options and separators, before any
	// credential I/O or Marshal can allocate an escaped multi-input payload.
	batchBytes := 0
	for index, text := range texts {
		if strings.TrimSpace(text) == "" || !utf8.ValidString(text) || utf8.RuneCountInString(text) > maxChars {
			return nil, fmt.Errorf("bedrock: empty, invalid or oversized embedding input")
		}
		if index%batchSize == 0 {
			batchBytes = 256
		}
		textBytes, fits := bedrockJSONStringSize(text, MaxMediaBytes-batchBytes-1)
		if !fits {
			return nil, fmt.Errorf("bedrock: oversized embedding request")
		}
		batchBytes += textBytes + 1
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	key, region, err := parseBedrockKey(local)
	if err != nil {
		return nil, err
	}
	endpoint, err := b.endpoint(region, modelID, bedrockSuffix(b.URLSuffix.Embedding, "invoke"), false)
	if err != nil {
		return nil, err
	}
	creds, err := b.resolveCredentials(requestContext(local), key, region)
	if err != nil {
		return nil, err
	}
	requestedDimension := dimension
	vectors := make([][]float64, 0, len(texts))
	for start := 0; start < len(texts); start += batchSize {
		end := min(start+batchSize, len(texts))
		body := map[string]any{}
		if titan {
			body["inputText"] = texts[start]
			if requestedDimension > 0 && modelID == "amazon.titan-embed-text-v2:0" {
				body["dimensions"] = requestedDimension
			}
		} else {
			body["texts"], body["input_type"] = texts[start:end], "search_document"
			if config != nil && config.Query {
				body["input_type"] = "search_query"
			}
			if modelID == "cohere.embed-v4:0" {
				body["embedding_types"] = []string{"float"}
				if requestedDimension > 0 {
					body["output_dimension"] = requestedDimension
				}
			}
		}
		raw, err := json.Marshal(body)
		if err != nil || len(raw) > MaxMediaBytes {
			return nil, fmt.Errorf("bedrock: oversized embedding request")
		}
		resp, err := b.signedRequest(requestContext(local), endpoint, raw, creds, region, false, false)
		if err != nil {
			return nil, err
		}
		var response struct {
			Embedding  []*float64      `json:"embedding"`
			Embeddings json.RawMessage `json:"embeddings"`
			Error      json.RawMessage `json:"error"`
		}
		decodeErr := decodeBedrockJSON(resp.Body, &response)
		resp.Body.Close()
		if err := requestContext(local).Err(); err != nil {
			return nil, err
		}
		if decodeErr != nil || len(response.Error) > 0 && string(response.Error) != "null" {
			return nil, fmt.Errorf("bedrock: invalid embedding response or provider error")
		}
		batch := [][]*float64{response.Embedding}
		if !titan {
			batch, err = decodeBedrockCohereVectors(response.Embeddings)
			if err != nil {
				return nil, err
			}
		}
		if len(batch) != end-start {
			return nil, fmt.Errorf("bedrock: embedding count mismatch")
		}
		for _, rawVector := range batch {
			if len(rawVector) == 0 {
				return nil, fmt.Errorf("bedrock: missing embedding")
			}
			if dimension == 0 {
				dimension = len(rawVector)
			}
			if len(rawVector) != dimension {
				return nil, fmt.Errorf("bedrock: inconsistent embedding dimensions")
			}
			vector := make([]float64, dimension)
			for i, value := range rawVector {
				if value == nil || math.IsNaN(*value) || math.IsInf(*value, 0) {
					return nil, fmt.Errorf("bedrock: invalid embedding value")
				}
				vector[i] = *value
			}
			vectors = append(vectors, vector)
		}
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return embeddingData(vectors), nil
}

func bedrockEmbeddingDimension(model string, dimension int) bool {
	if dimension == 0 {
		return true
	}
	switch model {
	case "amazon.titan-embed-text-v2:0":
		return dimension == 256 || dimension == 512 || dimension == 1024
	case "amazon.titan-embed-text-v1":
		return dimension == 1536
	case "cohere.embed-v4:0":
		return dimension == 256 || dimension == 512 || dimension == 1024 || dimension == 1536
	default:
		return dimension == 1024
	}
}

func decodeBedrockCohereVectors(raw json.RawMessage) ([][]*float64, error) {
	var vectors [][]*float64
	if len(raw) > 0 && json.Unmarshal(raw, &vectors) == nil && vectors != nil {
		return vectors, nil
	}
	var typed struct {
		Float [][]*float64 `json:"float"`
	}
	if json.Unmarshal(raw, &typed) != nil || typed.Float == nil {
		return nil, fmt.Errorf("bedrock: missing float embeddings")
	}
	return typed.Float, nil
}
