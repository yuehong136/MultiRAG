package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestBedrockEmbeddingTitanOrderAndSignature(t *testing.T) {
	var seen []string
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		verifyBedrockSignature(t, r, raw, "AKIDEXAMPLE", "controlled-secret", "controlled-token", "us-east-1", "bedrock")
		if r.URL.Path != "/model/amazon.titan-embed-text-v2:0/invoke" || r.Method != http.MethodPost {
			t.Error(r.URL, r.Method)
		}
		var body struct {
			Input     string `json:"inputText"`
			Dimension *int   `json:"dimensions"`
		}
		if json.Unmarshal(raw, &body) != nil || body.Dimension != nil {
			t.Error("invalid body or inferred dimension leaked to next request")
		}
		seen = append(seen, body.Input)
		fmt.Fprintf(w, `{"embedding":[%d,0],"inputTextTokenCount":0}`, len(seen))
	}))
	driver, err := NewModelFactory().CreateModelDriver("Bedrock", map[string]string{"default": server.URL}, URLSuffix{})
	if err != nil {
		t.Fatal(err)
	}
	model := "amazon.titan-embed-text-v2:0"
	data, err := NewEmbeddingModel(driver, &model, bedrockTestAPI()).Embed([]string{"one", "two"})
	if err != nil || !reflect.DeepEqual(seen, []string{"one", "two"}) || !reflect.DeepEqual(data, []EmbeddingData{{Index: 0, Embedding: []float64{1, 0}}, {Index: 1, Embedding: []float64{2, 0}}}) {
		t.Fatal(data, seen, err)
	}
}

func TestBedrockEmbeddingCohereBatchAndQuery(t *testing.T) {
	for _, model := range []string{"cohere.embed-english-v3", "cohere.embed-multilingual-v3", "cohere.embed-v4:0"} {
		t.Run(model, func(t *testing.T) {
			var sizes []int
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Texts     []string `json:"texts"`
					InputType string   `json:"input_type"`
					Dimension *int     `json:"output_dimension"`
					Types     []string `json:"embedding_types"`
				}
				if json.NewDecoder(r.Body).Decode(&body) != nil || body.InputType != "search_query" || body.Dimension != nil {
					t.Error("invalid request")
				}
				if model == "cohere.embed-v4:0" && !reflect.DeepEqual(body.Types, []string{"float"}) {
					t.Error(body.Types)
				}
				sizes = append(sizes, len(body.Texts))
				vectors := make([][]float64, len(body.Texts))
				for i, text := range body.Texts {
					var index int
					fmt.Sscanf(text, "input-%d", &index)
					vectors[i] = []float64{float64(index), 0}
				}
				if model == "cohere.embed-v4:0" {
					json.NewEncoder(w).Encode(map[string]any{"embeddings": map[string]any{"float": vectors}})
				} else {
					json.NewEncoder(w).Encode(map[string]any{"embeddings": vectors})
				}
			}))
			texts := make([]string, 97)
			for i := range texts {
				texts[i] = fmt.Sprintf("input-%d", i)
			}
			driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
			data, err := driver.Embed(&model, texts, bedrockTestAPI(), &EmbeddingConfig{Query: true})
			if err != nil || !reflect.DeepEqual(sizes, []int{96, 1}) || len(data) != 97 {
				t.Fatal(sizes, len(data), err)
			}
			for i, item := range data {
				if item.Index != i || item.Embedding[0] != float64(i) {
					t.Fatal(i, item)
				}
			}
		})
	}
}

func TestBedrockEmbeddingDimensions(t *testing.T) {
	for _, test := range []struct {
		model, field string
		dimension    int
	}{
		{"amazon.titan-embed-text-v1", "", 1536}, {"amazon.titan-embed-text-v2:0", "dimensions", 256}, {"cohere.embed-english-v3", "", 1024}, {"cohere.embed-v4:0", "output_dimension", 512},
	} {
		t.Run(test.model, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				if test.field != "" && body[test.field] != float64(test.dimension) {
					t.Error(body)
				}
				if test.field == "" && (body["dimensions"] != nil || body["output_dimension"] != nil) {
					t.Error(body)
				}
				vector := make([]float64, test.dimension)
				if strings.HasPrefix(test.model, "amazon.") {
					json.NewEncoder(w).Encode(map[string]any{"embedding": vector})
				} else {
					json.NewEncoder(w).Encode(map[string]any{"embeddings": [][]float64{vector}})
				}
			}))
			data, err := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{}).Embed(&test.model, []string{"text"}, bedrockTestAPI(), &EmbeddingConfig{Dimension: test.dimension})
			if err != nil || len(data) != 1 || len(data[0].Embedding) != test.dimension {
				t.Fatal(len(data), err)
			}
		})
	}
}

func TestBedrockEmbeddingInvalidInputsNoHTTP(t *testing.T) {
	var calls atomic.Int32
	server := providerTestServer(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
	model := "amazon.titan-embed-text-v2:0"
	if data, err := driver.Embed(nil, nil, nil, nil); err != nil || data == nil || len(data) != 0 {
		t.Fatal(data, err)
	}
	for _, test := range []struct {
		model     string
		texts     []string
		dimension int
	}{
		{"unknown", []string{"x"}, 0}, {"amazon.titan-embed-text-future", []string{"x"}, 0}, {model, []string{"x"}, -1}, {model, []string{"x"}, 12}, {model, []string{" "}, 0}, {model, []string{"valid", ""}, 0}, {model, []string{strings.Repeat("a", 50001)}, 0}, {model, []string{string([]byte{255})}, 0}, {"cohere.embed-english-v3", []string{strings.Repeat("a", 2049)}, 0},
	} {
		if data, err := driver.Embed(&test.model, test.texts, bedrockTestAPI(), &EmbeddingConfig{Dimension: test.dimension}); err == nil || data != nil {
			t.Fatal(test.model, data, err)
		}
	}
	if data, err := driver.Embed(&model, []string{"x"}, nil, nil); err == nil || data != nil {
		t.Fatal(data, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	api := bedrockTestAPI()
	api.Context = ctx
	if data, err := driver.Embed(&model, []string{"x"}, api, nil); !errors.Is(err, context.Canceled) || data != nil {
		t.Fatal(data, err)
	}
	if calls.Load() != 0 {
		t.Fatal("invalid input reached HTTP", calls.Load())
	}
}

func TestBedrockEmbeddingStrictResponses(t *testing.T) {
	for _, model := range []string{"amazon.titan-embed-text-v2:0", "cohere.embed-v4:0"} {
		valid := `{"embedding":[0,1]}`
		if strings.HasPrefix(model, "cohere") {
			valid = `{"embeddings":[[0,1]]}`
		}
		for _, raw := range []string{`{}`, `null`, `{"error":"private-secret"}`, valid + ` {}`, strings.Repeat(" ", MaxMediaBytes) + valid, `{"embedding":[null],"embeddings":[[null]]}`, `{"embedding":[1e999],"embeddings":[[1e999]]}`, `{"embedding":[],"embeddings":[]}`, `{"embeddings":{"int8":[[1]]}}`, `{"embedding":[null],"embedding":[0,1]}`, `{"embedding":[null],"Embedding":[0,1]}`, `{"embeddings":{"float":[[0,1]],"FLOAT":[[1,2]]}}`, `{"embedding":[0],"embeddings":[[0],[1]]}`} {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
			config := (*EmbeddingConfig)(nil)
			if strings.Contains(raw, `"embedding":[0]`) {
				config = &EmbeddingConfig{Dimension: 256}
			}
			data, err := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{}).Embed(&model, []string{"x"}, bedrockTestAPI(), config)
			if err == nil || data != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(model, len(raw), data, err)
			}
			server.Close()
		}
	}
}

func TestBedrockEmbeddingFailureClosesAndCancels(t *testing.T) {
	for _, mode := range []string{"http", "cancel", "dimension"} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int32
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			closed := make(chan struct{}, 2)
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				io.Copy(io.Discard, r.Body)
				if mode == "cancel" {
					w.WriteHeader(200)
					w.(http.Flusher).Flush()
					cancel()
					<-r.Context().Done()
					closed <- struct{}{}
					return
				}
				if mode == "http" {
					w.WriteHeader(403)
					fmt.Fprint(w, "private-secret")
					return
				}
				if calls.Load() == 1 {
					fmt.Fprint(w, `{"embedding":[0,1]}`)
				} else {
					fmt.Fprint(w, `{"embedding":[0]}`)
				}
			}))
			api := bedrockTestAPI()
			api.Context = ctx
			model := "amazon.titan-embed-text-v2:0"
			data, err := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{}).Embed(&model, []string{"first", "second"}, api, nil)
			if err == nil || data != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(data, err)
			}
			if mode == "cancel" {
				if !errors.Is(err, context.Canceled) {
					t.Fatal(err)
				}
				select {
				case <-closed:
				case <-time.After(time.Second):
					t.Fatal("request did not disconnect")
				}
			}
			if mode == "http" && calls.Load() != 1 {
				t.Fatal(calls.Load())
			}
		})
	}
}

func TestBedrockEmbeddingInstanceIsolation(t *testing.T) {
	var wg sync.WaitGroup
	for _, region := range []string{"us-east-1", "eu-west-1"} {
		key, secret := "KEY"+region, "SECRET"+region
		server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			raw, _ := io.ReadAll(r.Body)
			verifyBedrockSignature(t, r, raw, key, secret, "", region, "bedrock")
			fmt.Fprint(w, `{"embedding":[0,1]}`)
		}))
		driver := NewBedrockModel(map[string]string{region: server.URL}, URLSuffix{Embedding: "custom-invoke"})
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				raw, _ := json.Marshal(bedrockKey{AuthMode: "access_key_secret", Region: region, AccessKey: key, SecretKey: secret})
				blob := string(raw)
				model := "amazon.titan-embed-text-v2:0"
				if data, err := driver.Embed(&model, []string{"x"}, &APIConfig{APIKey: &blob}, nil); err != nil || len(data) != 1 {
					t.Error(data, err)
				}
			}()
		}
	}
	wg.Wait()
}
