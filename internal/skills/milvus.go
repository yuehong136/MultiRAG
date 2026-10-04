package skills

import (
	"context"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	me "github.com/milvus-io/milvus/client/v2/entity"
	"github.com/milvus-io/milvus/client/v2/index"
	mc "github.com/milvus-io/milvus/client/v2/milvusclient"
)

// MilvusStore deliberately bypasses the legacy DocEngine's unimplemented writes.
type MilvusStore struct{ Client *mc.Client }

func NewMilvus(ctx context.Context, cfg *mc.ClientConfig) (*MilvusStore, error) {
	c, e := mc.New(ctx, cfg)
	if e != nil {
		return nil, e
	}
	return &MilvusStore{c}, nil
}
func (m *MilvusStore) Build(ctx context.Context, name string, dim int, rows []IndexRow) error {
	if dim < 1 {
		return fault(422, "INVALID_DIMENSION")
	}
	schema := me.NewSchema().WithName(name)
	for _, f := range []struct {
		name string
		size int64
	}{{"id", 32}, {"skill_id", 32}, {"version_id", 32}, {"field", 16}, {"content_digest", 64}, {"text", 32768}} {
		field := me.NewField().WithName(f.name).WithDataType(me.FieldTypeVarChar).WithMaxLength(f.size)
		if f.name == "id" {
			field = field.WithIsPrimaryKey(true)
		}
		if f.name == "text" {
			field = field.WithEnableAnalyzer(true).WithAnalyzerParams(map[string]any{"type": "standard"})
		}
		schema = schema.WithField(field)
	}
	schema = schema.WithField(me.NewField().WithName("vector").WithDataType(me.FieldTypeFloatVector).WithDim(int64(dim))).WithField(me.NewField().WithName("sparse").WithDataType(me.FieldTypeSparseVector)).WithFunction(me.NewFunction().WithName("text_bm25").WithType(me.FunctionTypeBM25).WithInputFields("text").WithOutputFields("sparse"))
	sparse := index.NewGenericIndex("SPARSE_INVERTED_INDEX", map[string]string{"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25", "bm25_k1": "1.2", "bm25_b": "0.75"})
	if err := m.Client.CreateCollection(ctx, mc.NewCreateCollectionOption(name, schema).WithConsistencyLevel(me.ClStrong).WithIndexOptions(mc.NewCreateIndexOption(name, "vector", index.NewAutoIndex(me.COSINE)), mc.NewCreateIndexOption(name, "sparse", sparse))); err != nil {
		return err
	}
	for start := 0; start < len(rows); start += 64 {
		end := min(start+64, len(rows))
		batch := rows[start:end]
		ids, skills, versions, fields, texts, digests := []string{}, []string{}, []string{}, []string{}, []string{}, []string{}
		vectors := [][]float32{}
		for _, r := range batch {
			if len(r.Vector) != dim {
				return fault(422, "INVALID_DIMENSION")
			}
			ids = append(ids, r.ID)
			skills = append(skills, r.SkillID)
			versions = append(versions, r.VersionID)
			fields = append(fields, r.Field)
			texts = append(texts, r.Text)
			digests = append(digests, r.Digest)
			vectors = append(vectors, r.Vector)
		}
		result, e := m.Client.Insert(ctx, mc.NewColumnBasedInsertOption(name).WithVarcharColumn("id", ids).WithVarcharColumn("skill_id", skills).WithVarcharColumn("version_id", versions).WithVarcharColumn("field", fields).WithVarcharColumn("text", texts).WithVarcharColumn("content_digest", digests).WithFloatVectorColumn("vector", dim, vectors))
		if e != nil {
			return e
		}
		if result.InsertCount != int64(len(batch)) {
			return fmt.Errorf("incomplete index insert")
		}
	}
	task, e := m.Client.LoadCollection(ctx, mc.NewLoadCollectionOption(name))
	if e != nil {
		return e
	}
	if e = task.Await(ctx); e != nil {
		return e
	}
	return m.Verify(ctx, name, rows)
}
func columnString(r *mc.ResultSet, name string, i int) (string, error) {
	c := r.GetColumn(name)
	if c == nil {
		return "", fmt.Errorf("index response missing %s", name)
	}
	v, e := c.Get(i)
	if e != nil {
		return "", e
	}
	str, ok := v.(string)
	if !ok {
		return "", fmt.Errorf("invalid index field %s", name)
	}
	return str, nil
}
func (m *MilvusStore) Verify(ctx context.Context, name string, rows []IndexRow) error {
	total, e := m.Client.Query(ctx, mc.NewQueryOption(name).WithOutputFields("count(*)").WithConsistencyLevel(me.ClStrong))
	if e != nil {
		return e
	}
	countColumn := total.GetColumn("count(*)")
	if countColumn == nil {
		return fmt.Errorf("index count missing")
	}
	count, e := countColumn.Get(0)
	if e != nil {
		return e
	}
	if count != int64(len(rows)) {
		return fmt.Errorf("index total count mismatch")
	}

	for start := 0; start < len(rows); start += 100 {
		end := min(start+100, len(rows))
		ids := []string{}
		expected := map[string]IndexRow{}
		for _, r := range rows[start:end] {
			ids = append(ids, strconv.Quote(r.ID))
			expected[r.ID] = r
		}
		r, e := m.Client.Query(ctx, mc.NewQueryOption(name).WithFilter("id in ["+strings.Join(ids, ",")+"]").WithOutputFields("id", "version_id", "skill_id", "field", "content_digest", "text", "vector").WithConsistencyLevel(me.ClStrong).WithLimit(100))
		if e != nil {
			return e
		}
		if r.ResultCount != len(expected) {
			return fmt.Errorf("index readback count mismatch")
		}
		for i := 0; i < r.ResultCount; i++ {
			id, e := columnString(&r, "id", i)
			if e != nil {
				return e
			}
			digest, e := columnString(&r, "content_digest", i)
			if e != nil {
				return e
			}
			text, e := columnString(&r, "text", i)
			if e != nil {
				return e
			}
			version, e := columnString(&r, "version_id", i)
			if e != nil {
				return e
			}
			skill, e := columnString(&r, "skill_id", i)
			if e != nil {
				return e
			}
			field, e := columnString(&r, "field", i)
			if e != nil {
				return e
			}
			vectors := r.GetColumn("vector")
			if vectors == nil {
				return fmt.Errorf("index vector missing")
			}
			raw, e := vectors.Get(i)
			if e != nil {
				return e
			}
			vector, valid := raw.(me.FloatVector)
			want, ok := expected[id]
			if !ok || digest != want.Digest || text != want.Text || version != want.VersionID || skill != want.SkillID || field != want.Field || !valid || len(vector) != len(want.Vector) {
				return fmt.Errorf("index readback mismatch")
			}
			for j, value := range vector {
				if value != want.Vector[j] {
					return fmt.Errorf("index vector mismatch")
				}
			}
		}
	}
	if len(rows) > 0 {
		sample := rows[0]
		results, e := m.Client.Search(ctx, mc.NewSearchOption(name, 1, []me.Vector{me.FloatVector(sample.Vector)}).WithANNSField("vector").WithFilter("id == "+strconv.Quote(sample.ID)).WithOutputFields("id").WithConsistencyLevel(me.ClStrong))
		if e != nil {
			return e
		}
		if len(results) != 1 || results[0].Err != nil || results[0].ResultCount != 1 {
			return fmt.Errorf("index dense search readback failed")
		}
		id, e := columnString(&results[0], "id", 0)
		if e != nil || id != sample.ID {
			return fmt.Errorf("index dense search identity mismatch")
		}
	}
	return nil
}
func (m *MilvusStore) Search(ctx context.Context, name, query, mode string, vector []float32, cfg Config) (SearchHits, error) {
	type scores struct {
		skill, version, text string
		best                 float64
		kw, vec              map[string]float64
	}
	byVersion := map[string]*scores{}
	weightSum := 0.0
	weights := map[string]float64{}
	truncated := false
	rawLimit := min(16384, max(cfg.TopK*4, 100))
	for _, field := range []string{"name", "tags", "description", "content"} {
		raw := asJSON(cfg.Fields[field])
		enabled, _ := raw["enabled"].(bool)
		weight, _ := number(raw["weight"])
		if !enabled || weight <= 0 {
			continue
		}
		weights[field] = weight
		weightSum += weight
		for _, leg := range []string{"keyword", "vector"} {
			if mode != "hybrid" && mode != leg {
				continue
			}
			ann := "sparse"
			vectors := []me.Vector{me.Text(query)}
			if leg == "vector" {
				ann = "vector"
				vectors = []me.Vector{me.FloatVector(vector)}
			}
			result, e := m.Client.Search(ctx, mc.NewSearchOption(name, rawLimit, vectors).WithANNSField(ann).WithFilter("field == "+strconv.Quote(field)).WithOutputFields("skill_id", "version_id", "text").WithConsistencyLevel(me.ClStrong))
			if e != nil {
				return SearchHits{}, e
			}
			for _, r := range result {
				if r.Err != nil {
					return SearchHits{}, r.Err
				}
				if r.ResultCount >= rawLimit {
					truncated = true
				}
				for i := 0; i < r.ResultCount; i++ {
					skill, e := columnString(&r, "skill_id", i)
					if e != nil {
						return SearchHits{}, e
					}
					version, e := columnString(&r, "version_id", i)
					if e != nil {
						return SearchHits{}, e
					}
					text, e := columnString(&r, "text", i)
					if e != nil {
						return SearchHits{}, e
					}
					if i >= len(r.Scores) {
						return SearchHits{}, fmt.Errorf("index response missing score")
					}
					sc := float64(r.Scores[i])
					if math.IsNaN(sc) || math.IsInf(sc, 0) {
						return SearchHits{}, fmt.Errorf("invalid index score")
					}
					if leg == "keyword" {
						sc = max(0, sc)
						sc = sc / (1 + sc)
					} else {
						sc = min(1, max(0, sc))
					}
					v := byVersion[version]
					if v == nil {
						v = &scores{skill: skill, version: version, kw: map[string]float64{}, vec: map[string]float64{}}
						byVersion[version] = v
					}
					if v.text == "" || sc > v.best {
						v.best = sc
						v.text = text
					}
					if leg == "keyword" {
						v.kw[field] = max(v.kw[field], sc)
					} else {
						v.vec[field] = max(v.vec[field], sc)
					}
				}
			}
		}
	}
	if len(byVersion) > cfg.TopK {
		truncated = true
	}
	hits := []Hit{}
	for _, v := range byVersion {
		kw, vec := 0.0, 0.0
		for field, w := range weights {
			kw += w * v.kw[field]
			vec += w * v.vec[field]
		}
		if weightSum > 0 {
			kw /= weightSum
			vec /= weightSum
		}
		score := kw
		if mode == "vector" {
			score = vec
		} else if mode == "hybrid" {
			score = (1-cfg.VectorWeight)*kw + cfg.VectorWeight*vec
		}
		if score >= cfg.SimilarityThreshold {
			hits = append(hits, Hit{v.skill, v.version, v.text, score})
		}
	}
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].Score == hits[j].Score {
			return hits[i].SkillID < hits[j].SkillID
		}
		return hits[i].Score > hits[j].Score
	})
	if len(hits) > cfg.TopK {
		truncated = true
		hits = hits[:cfg.TopK]
	}
	return SearchHits{hits, truncated}, nil
}
func (m *MilvusStore) Delete(ctx context.Context, name string, versions []string) error {
	exists, e := m.Client.HasCollection(ctx, mc.NewHasCollectionOption(name))
	if e != nil || !exists {
		return e
	}
	for _, id := range versions {
		filter := "version_id == " + strconv.Quote(id)
		if _, e = m.Client.Delete(ctx, mc.NewDeleteOption(name).WithExpr(filter)); e != nil {
			return e
		}
		r, e := m.Client.Query(ctx, mc.NewQueryOption(name).WithFilter(filter).WithOutputFields("id").WithLimit(1).WithConsistencyLevel(me.ClStrong))
		if e != nil {
			return e
		}
		if r.ResultCount != 0 {
			return fmt.Errorf("index deletion not visible")
		}
	}
	return nil
}
func (m *MilvusStore) Drop(ctx context.Context, name string) error {
	exists, e := m.Client.HasCollection(ctx, mc.NewHasCollectionOption(name))
	if e != nil || !exists {
		return e
	}
	if e = m.Client.DropCollection(ctx, mc.NewDropCollectionOption(name)); e != nil {
		return e
	}
	exists, e = m.Client.HasCollection(ctx, mc.NewHasCollectionOption(name))
	if e != nil {
		return e
	}
	if exists {
		return fmt.Errorf("collection deletion not visible")
	}
	return nil
}
