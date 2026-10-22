package cli

import (
	"fmt"
	"multirag/internal/entity/models"
)

// EmbeddingsResponse retains vectors while rendering a compact dimension summary.
type EmbeddingsResponse struct {
	Code         int                    `json:"code"`
	Data         []models.EmbeddingData `json:"data"`
	Message      string                 `json:"message"`
	Duration     float64
	outputFormat OutputFormat
}

func (r *EmbeddingsResponse) Type() string                        { return "common" }
func (r *EmbeddingsResponse) TimeCost() float64                   { return r.Duration }
func (r *EmbeddingsResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }
func (r *EmbeddingsResponse) PrintOut() {
	if r.Code != 0 {
		fmt.Printf("ERROR\n%d, %s\n", r.Code, r.Message)
		return
	}
	data := make([]map[string]interface{}, len(r.Data))
	for i, item := range r.Data {
		data[i] = map[string]interface{}{"index": item.Index, "dimension": len(item.Embedding)}
	}
	PrintTableSimpleByFormat(data, r.outputFormat)
}
