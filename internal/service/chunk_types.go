package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// SourcedChunk is the allowlisted public citation projection. Raw engine vectors
// can never enter a reference; citation hydration remains a separate scoped read.
type SourcedChunk struct {
	ID               string `json:"id"`
	Content          string `json:"content"`
	DocID            string `json:"document_id"`
	DocName          string `json:"document_name"`
	DatasetID        string `json:"dataset_id"`
	ImageID          string `json:"image_id"`
	Positions        any    `json:"positions"`
	URL              string `json:"url"`
	Similarity       any    `json:"similarity"`
	VectorSimilarity any    `json:"vector_similarity"`
	TermSimilarity   any    `json:"term_similarity"`
	DocType          any    `json:"doc_type"`
	DocumentMetadata any    `json:"document_metadata"`
	RowID            any    `json:"row_id"`
}

func NewSourcedChunks(rows []map[string]interface{}) ([]SourcedChunk, error) {
	chunks := make([]SourcedChunk, 0, len(rows))
	seen := map[string]bool{}
	for _, row := range rows {
		text := answerString(row, "content_with_weight", "content", "content_ltks")
		if strings.TrimSpace(text) == "" {
			continue
		}
		id := answerString(row, "chunk_id", "id", "_id")
		if !exactVectorID(id) || strings.ContainsAny(id, "[]\r\n") || seen[id] {
			return nil, fmt.Errorf("invalid or ambiguous citation chunk ID")
		}
		seen[id] = true
		positions := row["positions"]
		if positions == nil {
			positions = row["position_int"]
		}
		if positions == nil {
			positions = []any{}
		}
		chunk := SourcedChunk{ID: id, Content: text, DocID: answerString(row, "doc_id", "document_id"), DocName: answerString(row, "docnm_kwd", "document_name"), DatasetID: referenceKBID(row["kb_id"]), ImageID: answerString(row, "img_id", "image_id"), Positions: positions, URL: answerString(row, "url"), Similarity: row["similarity"], VectorSimilarity: row["vector_similarity"], TermSimilarity: row["term_similarity"], DocType: row["doc_type"], DocumentMetadata: row["document_metadata"]}
		chunk.RowID = row["row_id"]
		if chunk.DocType == nil {
			chunk.DocType = row["doc_type_kwd"]
		}
		if chunk.DatasetID == "" {
			chunk.DatasetID = answerString(row, "dataset_id")
		}
		// UseNumber keeps exact metadata operands, including IDs above 2^53.
		data, err := json.Marshal(chunk)
		if err != nil {
			return nil, err
		}
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		if err := decoder.Decode(&chunk); err != nil {
			return nil, err
		}
		chunks = append(chunks, chunk)
	}
	return chunks, nil
}

func answerString(row map[string]interface{}, keys ...string) string {
	for _, key := range keys {
		if value, ok := row[key].(string); ok && value != "" {
			return value
		}
	}
	return ""
}

func ChunksFormat(chunks []SourcedChunk) []map[string]interface{} {
	result := make([]map[string]interface{}, 0, len(chunks))
	for _, chunk := range chunks {
		result = append(result, map[string]interface{}{"id": chunk.ID, "row_id": chunk.RowID, "content": chunk.Content, "document_id": chunk.DocID, "document_name": chunk.DocName, "dataset_id": chunk.DatasetID, "image_id": chunk.ImageID, "positions": chunk.Positions, "url": chunk.URL, "similarity": chunk.Similarity, "vector_similarity": chunk.VectorSimilarity, "term_similarity": chunk.TermSimilarity, "doc_type": chunk.DocType, "document_metadata": chunk.DocumentMetadata})
	}
	return result
}

func answerReference(chunks []SourcedChunk) map[string]interface{} {
	aggs := []map[string]interface{}{}
	byDoc := map[string]int{}
	for _, chunk := range chunks {
		if chunk.DocID == "" {
			continue
		}
		key := chunk.DatasetID + "\x00" + chunk.DocID
		if i, ok := byDoc[key]; ok {
			aggs[i]["count"] = aggs[i]["count"].(int) + 1
			continue
		}
		byDoc[key] = len(aggs)
		aggs = append(aggs, map[string]interface{}{"doc_id": chunk.DocID, "doc_name": chunk.DocName, "kb_id": chunk.DatasetID, "count": 1})
	}
	return map[string]interface{}{"chunks": ChunksFormat(chunks), "doc_aggs": aggs}
}
