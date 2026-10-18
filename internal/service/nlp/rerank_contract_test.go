package nlp

import (
	"errors"
	"multirag/internal/entity/models"
	"reflect"
	"testing"
)

type structuredReranker struct {
	models.ModelDriver
	response *models.RerankResponse
	err      error
}

func (d *structuredReranker) Rerank(_ *string, _ string, _ []string, _ *models.APIConfig, _ *models.RerankConfig) (*models.RerankResponse, error) {
	return d.response, d.err
}
func TestRerankByModelIndexedAndFailureResponses(t *testing.T) {
	chunks := []map[string]interface{}{{"content_ltks": "first"}, {"content_ltks": "second"}}
	cases := []struct {
		driver *structuredReranker
		want   []float64
	}{
		{&structuredReranker{response: &models.RerankResponse{Data: []models.RerankResult{{Index: 1, RelevanceScore: .8}, {Index: 0, RelevanceScore: 0}}}}, []float64{0, .8}},
		{&structuredReranker{err: errors.New("provider unavailable")}, []float64{0, 0}},
		{&structuredReranker{}, []float64{0, 0}},
		{&structuredReranker{response: &models.RerankResponse{Data: []models.RerankResult{{Index: 0, RelevanceScore: 1}, {Index: 0, RelevanceScore: .2}}}}, []float64{0, 0}},
	}
	for _, test := range cases {
		_, _, scores := RerankByModel(models.NewRerankModel(test.driver, nil, nil), chunks, "q", 0, 1, "content_ltks", nil, nil)
		if !reflect.DeepEqual(scores, test.want) {
			t.Fatal(scores, test.want)
		}
	}
}
