package nlp

import (
	"context"
	"errors"
	"math"
	"reflect"
	"testing"

	"multirag/internal/entity/models"
)

func TestNormalizeRerankScoresContract(t *testing.T) {
	cases := []struct {
		name     string
		in, want []float64
	}{
		{"nil", nil, nil},
		{"empty", []float64{}, []float64{}},
		{"calibrated", []float64{0, .8, .2, 1}, []float64{0, .8, .2, 1}},
		{"flat calibrated", []float64{.8, .8}, []float64{.8, .8}},
		{"mixed logits", []float64{10, -3, 0}, []float64{1, 0, 3.0 / 13}},
		{"negative logits", []float64{-1, -5, -3}, []float64{1, 0, .5}},
		{"single high", []float64{5}, []float64{1}},
		{"single low", []float64{-3}, []float64{0}},
		{"near constant", []float64{5, 5.0001}, []float64{1, 1}},
		{"finite extremes", []float64{-math.MaxFloat64, 0, math.MaxFloat64}, []float64{0, .5, 1}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := NormalizeRerankScores(tc.in)
			if err != nil || (got == nil) != (tc.want == nil) || len(got) != len(tc.want) {
				t.Fatalf("%v %v", got, err)
			}
			for i := range got {
				if math.Abs(got[i]-tc.want[i]) > 1e-12 {
					t.Fatalf("%v, want %v", got, tc.want)
				}
			}
			if len(got) > 0 && &got[0] != &tc.in[0] {
				t.Fatal("normalization allocated new backing array")
			}
		})
	}
	for _, bad := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		in := []float64{3, bad, -2}
		if got, err := NormalizeRerankScores(in); err == nil || got != nil || in[0] != 3 || in[2] != -2 {
			t.Fatal("invalid input mutated or accepted", got, err)
		}
	}
}

type capturedReranker struct {
	models.ModelDriver
	texts  []string
	scores []float64
	err    error
	cancel context.CancelFunc
}

func (d *capturedReranker) Rerank(_ *string, _ string, texts []string, _ *models.APIConfig, _ *models.RerankConfig) (*models.RerankResponse, error) {
	d.texts = append([]string(nil), texts...)
	if d.cancel != nil {
		d.cancel()
	}
	response := &models.RerankResponse{Data: make([]models.RerankResult, len(d.scores))}
	for i, score := range d.scores {
		response.Data[i] = models.RerankResult{Index: i, RelevanceScore: score}
	}
	return response, d.err
}

func TestRerankUsesNaturalTextAndNormalizedBlend(t *testing.T) {
	driver := &capturedReranker{scores: []float64{-5, 10}}
	chunks := []map[string]interface{}{
		{"content_with_weight": "自然 文本 ABC 中文", "content_ltks": "token 一 token", "title_tks": "title"},
		{"content_with_weight": "Keep Spaces Between Words", "content_ltks": "second", "question_tks": "extra"},
	}
	sim, term, vector, err := RerankByModel(context.Background(), models.NewRerankModel(driver, nil, nil), chunks, "q", .3, .7, "content_ltks", nil, nil)
	if err != nil || !reflect.DeepEqual(driver.texts, []string{"自然 文本 ABC 中文", "Keep Spaces Between Words"}) || !reflect.DeepEqual(vector, []float64{0, 1}) || !reflect.DeepEqual(sim, []float64{0, .7}) || !reflect.DeepEqual(term, []float64{0, 0}) {
		t.Fatalf("%v %v %v %v %v", driver.texts, sim, term, vector, err)
	}
	for _, weight := range []float64{-1, math.NaN(), math.Inf(1), 1.01} {
		driver.texts = nil
		if _, _, _, err = RerankByModel(context.Background(), models.NewRerankModel(driver, nil, nil), chunks, "q", 1-weight, weight, "content_ltks", nil, nil); err == nil || driver.texts != nil {
			t.Fatal("invalid weight reached provider")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	driver.cancel = cancel
	if _, _, _, err = RerankByModel(ctx, models.NewRerankModel(driver, nil, nil), chunks, "q", 0, 1, "content_ltks", nil, nil); !errors.Is(err, context.Canceled) {
		t.Fatal("cancellation lost", err)
	}
}

func TestRerankTokensAndRankFeaturesRemainDeterministic(t *testing.T) {
	if got := extractContentTokens(map[string]interface{}{"content_ltks": "中 文 重复 重复"}, "content_ltks"); !reflect.DeepEqual(got, []string{"中", "文", "重复", "重复"}) {
		t.Fatal(got)
	}
	if got := extractTitleTokens(map[string]interface{}{"title_tks": "中 文"}); !reflect.DeepEqual(got, []string{"中", "文"}) {
		t.Fatal(got)
	}
	chunks := []map[string]interface{}{{"pagerank_fea": .1, "tag_feas": `{"x":1}`}}
	got := applyRankFeatureScores(chunks, []float64{.2}, map[string]float64{"pagerank_fea": 10})
	if math.Abs(got[0]-.3) > 1e-12 {
		t.Fatal("pagerank-only query produced invalid score", got)
	}
	if got := ArgsortDescending([]float64{1, 1, 0, 1}); !reflect.DeepEqual(got, []int{0, 1, 3, 2}) {
		t.Fatal("ties reordered", got)
	}
}

func TestInfinityFallbackRejectsMissingAndInvalidScores(t *testing.T) {
	for _, chunk := range []map[string]interface{}{
		{}, {"_score": nil}, {"SCORE": "0.8"}, {"score": math.NaN()}, {"similarity": math.Inf(1)},
	} {
		if sim, term, vector, err := RerankInfinityFallback([]map[string]interface{}{chunk}); err == nil || sim != nil || term != nil || vector != nil {
			t.Fatal("missing/invalid score became success", sim, term, vector, err)
		}
	}
	for _, value := range []interface{}{0.0, 0, float32(.5), .8} {
		sim, term, vector, err := RerankInfinityFallback([]map[string]interface{}{{"_score": value}})
		if err != nil || len(sim) != 1 || !reflect.DeepEqual(sim, term) || !reflect.DeepEqual(sim, vector) {
			t.Fatal("valid fused score rejected", sim, err)
		}
	}
}

func TestRetrievalVectorZeroTopUsesExplicitCandidateDefault(t *testing.T) {
	model := models.NewEmbeddingModel(&retrievalEmbeddingDriver{vectors: [][]float64{{1, 0}}}, nil, nil)
	expression, err := NewRetrievalService(nil).GetVector("question", model, 0, 0)
	if err != nil || expression.TopN != 1024 || expression.ExtraOptions["similarity"] != 0.0 {
		t.Fatal("legal zero top/threshold lost", expression, err)
	}
	if expression, err := NewRetrievalService(nil).GetVector("question", model, -1, 0); err == nil || expression != nil {
		t.Fatal("negative top accepted", expression, err)
	}
}
