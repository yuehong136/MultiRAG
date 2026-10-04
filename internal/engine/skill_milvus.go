package engine

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	me "github.com/milvus-io/milvus/client/v2/entity"
	"github.com/milvus-io/milvus/client/v2/index"
	mc "github.com/milvus-io/milvus/client/v2/milvusclient"
	"multirag/internal/engine/types"
)

// SkillMilvus preserves the upstream one-document-per-skill and stable index
// naming contract. Physical replacement collections are private to this adapter.
type SkillMilvus struct {
	Client   *mc.Client
	redirect map[string]string
}

func NewSkillMilvus(ctx context.Context, cfg *mc.ClientConfig) (*SkillMilvus, error) {
	c, e := mc.New(ctx, cfg)
	return &SkillMilvus{Client: c}, e
}
func (m *SkillMilvus) GetType() string { return "milvus" }
func (m *SkillMilvus) physical(name string) string {
	if p := m.redirect[name]; p != "" {
		return p
	}
	return name
}

// Resolve the alias before data operations: the SDK caches collection IDs by
// request name, which otherwise survive AlterAlias and target a retired ID.
func (m *SkillMilvus) resolve(ctx context.Context, name string) (string, error) {
	if v := m.redirect[name]; v != "" {
		return v, nil
	}
	a, e := m.Client.DescribeAlias(ctx, mc.NewDescribeAliasOption(name))
	if e != nil {
		return "", e
	}
	return a.CollectionName, nil
}
func (m *SkillMilvus) TableExists(ctx context.Context, name string) (bool, error) {
	return m.Client.HasCollection(ctx, mc.NewHasCollectionOption(m.physical(name)))
}
func (m *SkillMilvus) create(ctx context.Context, name string, dim int) error {
	if dim < 1 || dim > 32768 {
		return fmt.Errorf("EMBEDDING_INVALID")
	}
	schema := me.NewSchema().WithName(name)
	for _, field := range []struct {
		name string
		size int64
	}{{"id", 32}, {"doc_id", 2048}, {"digest", 64}, {"kind", 8}, {"group_id", 32}, {"payload", 32768}, {"text", 32768}} {
		f := me.NewField().WithName(field.name).WithDataType(me.FieldTypeVarChar).WithMaxLength(field.size)
		if field.name == "id" {
			f = f.WithIsPrimaryKey(true)
		}
		if field.name == "text" {
			f = f.WithEnableAnalyzer(true).WithAnalyzerParams(map[string]any{"type": "standard"})
		}
		schema = schema.WithField(f)
	}
	schema = schema.WithField(me.NewField().WithName("ordinal").WithDataType(me.FieldTypeInt64))

	schema = schema.WithField(me.NewField().WithName("vector").WithDataType(me.FieldTypeFloatVector).WithDim(int64(dim)))
	schema = schema.WithField(me.NewField().WithName("sparse").WithDataType(me.FieldTypeSparseVector)).WithFunction(me.NewFunction().WithName("bm25").WithType(me.FunctionTypeBM25).WithInputFields("text").WithOutputFields("sparse"))
	sparse := index.NewGenericIndex("SPARSE_INVERTED_INDEX", map[string]string{"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25", "bm25_k1": "1.2", "bm25_b": "0.75"})
	if e := m.Client.CreateCollection(ctx, mc.NewCreateCollectionOption(name, schema).WithConsistencyLevel(me.ClStrong).WithIndexOptions(mc.NewCreateIndexOption(name, "vector", index.NewAutoIndex(me.COSINE)), mc.NewCreateIndexOption(name, "sparse", sparse))); e != nil {
		return e
	}
	task, e := m.Client.LoadCollection(ctx, mc.NewLoadCollectionOption(name))
	if e != nil {
		return e
	}
	return task.Await(ctx)
}
func (m *SkillMilvus) CreateDataset(ctx context.Context, name, dataset string, dim int, parser string) error {
	exists, e := m.TableExists(ctx, name)
	if e != nil || exists {
		return e
	}
	if m.redirect[name] != "" {
		return m.create(ctx, m.physical(name), dim)
	}
	return m.ReplaceSkillIndex(ctx, name, dim, func(SkillDocEngine) error { return nil })
}
func (m *SkillMilvus) ReplaceSkillIndex(ctx context.Context, name string, dim int, build func(SkillDocEngine) error) error {
	physical := skillCollectionPrefix(name) + strconv.FormatInt(time.Now().Unix(), 10) + "_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if e := m.create(ctx, physical, dim); e != nil {
		return e
	}
	staged := &SkillMilvus{Client: m.Client, redirect: map[string]string{name: physical}}
	if e := build(staged); e != nil {
		_ = m.Client.DropCollection(context.WithoutCancel(ctx), mc.NewDropCollectionOption(physical))
		return e
	}
	aliases, e := m.Client.ListAliases(ctx, mc.NewListAliasesOption(""))
	if e != nil {
		return e
	}
	old := ""
	found := false
	for _, a := range aliases {
		if a == name {
			found = true
			break
		}
	}
	if found {
		a, e := m.Client.DescribeAlias(ctx, mc.NewDescribeAliasOption(name))
		if e != nil {
			return e
		}
		old = a.CollectionName
		e = m.Client.AlterAlias(ctx, mc.NewAlterAliasOption(name, physical))
	} else {
		e = m.Client.CreateAlias(ctx, mc.NewCreateAliasOption(physical, name))
	}
	if e != nil {
		return e
	}
	// Alias publication is authoritative; old collection cleanup can be retried
	// separately without pretending a failed build succeeded.
	if old != "" {
		if e = m.Client.DropCollection(ctx, mc.NewDropCollectionOption(old)); e != nil {
			return fmt.Errorf("index published but retired collection cleanup failed: %w", e)
		}
	}
	return nil
}
func skillCollectionPrefix(name string) string {
	sum := sha256.Sum256([]byte(name))
	return fmt.Sprintf("gskill_%x_", sum[:8])
}
func (m *SkillMilvus) DropTable(ctx context.Context, name string) error {
	// Collection names persist the space identity. Recovery can find all retired
	// or abandoned replacements even after an alias has already disappeared.
	names, e := m.Client.ListCollections(ctx, mc.NewListCollectionOption())
	if e != nil {
		return e
	}
	for _, physical := range names {
		if strings.HasPrefix(physical, skillCollectionPrefix(name)) {
			aliases, err := m.Client.ListAliases(ctx, mc.NewListAliasesOption(physical))
			if err != nil {
				return err
			}
			for _, alias := range aliases {
				if e = m.Client.DropAlias(ctx, mc.NewDropAliasOption(alias)); e != nil {
					return e
				}
			}
			if e = m.Client.DropCollection(ctx, mc.NewDropCollectionOption(physical)); e != nil {
				return e
			}
		}
	}
	return nil
}

// A head is the atomic visibility pointer for one upstream logical document.
// Text and metadata fragments are prepared and verified before replacing it.
type skillFragment struct {
	id, doc, digest, kind, group, payload, text string
	ordinal                                     int64
	vector                                      []float32
}

func fragmentID(value string) string {
	sum := sha256.Sum256([]byte(value))
	return fmt.Sprintf("%x", sum[:16])
}
func splitSkillUTF8(text string, budget int) ([]string, error) {
	if !utf8.ValidString(text) || budget < 4 {
		return nil, fmt.Errorf("INVALID_INDEX_TEXT")
	}
	out := []string{}
	for len(text) > 0 {
		n := min(len(text), budget)
		for n < len(text) && !utf8.RuneStart(text[n]) {
			n--
		}
		out = append(out, text[:n])
		text = text[n:]
	}
	return out, nil
}
func resultString(result *mc.ResultSet, field string, i int) (string, error) {
	c := result.GetColumn(field)
	if c == nil {
		return "", fmt.Errorf("index response missing %s", field)
	}
	v, e := c.Get(i)
	if e != nil {
		return "", e
	}
	text, ok := v.(string)
	if !ok {
		return "", fmt.Errorf("invalid index field %s", field)
	}
	return text, nil
}
func (m *SkillMilvus) writeFragments(ctx context.Context, name string, rows []skillFragment) error {
	for start := 0; start < len(rows); start += 64 {
		batch := rows[start:min(start+64, len(rows))]
		ids, docs, digests, kinds, groups, payloads, texts := []string{}, []string{}, []string{}, []string{}, []string{}, []string{}, []string{}
		ordinals := []int64{}
		vectors := [][]float32{}
		for _, row := range batch {
			ids = append(ids, row.id)
			docs = append(docs, row.doc)
			digests = append(digests, row.digest)
			kinds = append(kinds, row.kind)
			groups = append(groups, row.group)
			payloads = append(payloads, row.payload)
			texts = append(texts, row.text)
			ordinals = append(ordinals, row.ordinal)
			vectors = append(vectors, row.vector)
		}
		result, e := m.Client.Upsert(ctx, mc.NewColumnBasedInsertOption(name).WithVarcharColumn("id", ids).WithVarcharColumn("doc_id", docs).WithVarcharColumn("digest", digests).WithVarcharColumn("kind", kinds).WithVarcharColumn("group_id", groups).WithVarcharColumn("payload", payloads).WithVarcharColumn("text", texts).WithInt64Column("ordinal", ordinals).WithFloatVectorColumn("vector", len(vectors[0]), vectors))
		if e != nil {
			return e
		}
		if result.UpsertCount != int64(len(batch)) {
			return fmt.Errorf("incomplete fragment write")
		}
		encoded, _ := json.Marshal(ids)
		read, e := m.Client.Query(ctx, mc.NewQueryOption(name).WithFilter("id in "+string(encoded)).WithOutputFields("id", "doc_id", "digest", "kind", "group_id", "payload", "text", "vector", "ordinal").WithLimit(len(batch)).WithConsistencyLevel(me.ClStrong))
		if e != nil {
			return e
		}
		if read.ResultCount != len(batch) {
			return fmt.Errorf("fragment readback count mismatch")
		}
		expected := map[string]skillFragment{}
		for _, row := range batch {
			expected[row.id] = row
		}
		for i := 0; i < read.ResultCount; i++ {
			id, e := resultString(&read, "id", i)
			if e != nil {
				return e
			}
			row, ok := expected[id]
			if !ok {
				return fmt.Errorf("unexpected fragment")
			}
			for key, want := range map[string]string{"doc_id": row.doc, "digest": row.digest, "kind": row.kind, "group_id": row.group, "payload": row.payload, "text": row.text} {
				got, e := resultString(&read, key, i)
				if e != nil || got != want {
					return fmt.Errorf("fragment %s readback mismatch", key)
				}
			}
			ordinal, e := read.GetColumn("ordinal").Get(i)
			if e != nil || ordinal != row.ordinal {
				return fmt.Errorf("fragment ordinal mismatch")
			}
			v, e := read.GetColumn("vector").Get(i)
			if e != nil {
				return e
			}
			vector, ok := v.(me.FloatVector)
			if !ok || len(vector) != len(row.vector) {
				return fmt.Errorf("fragment vector mismatch")
			}
			for j, f := range vector {
				if f != row.vector[j] {
					return fmt.Errorf("fragment vector mismatch")
				}
			}
		}
	}
	return nil
}
func skillDocumentFragments(id string, doc map[string]interface{}) ([]skillFragment, skillFragment, error) {
	var vector []float32
	payload := map[string]interface{}{}
	for key, value := range doc {
		if strings.HasPrefix(key, "q_") && strings.HasSuffix(key, "_vec") {
			raw, ok := value.([]float64)
			if !ok {
				return nil, skillFragment{}, fmt.Errorf("EMBEDDING_INVALID")
			}
			norm := 0.0
			for _, f := range raw {
				if math.IsNaN(f) || math.IsInf(f, 0) || math.Abs(f) > math.MaxFloat32 {
					return nil, skillFragment{}, fmt.Errorf("EMBEDDING_INVALID")
				}
				norm += f * f
				vector = append(vector, float32(f))
			}
			if norm == 0 {
				return nil, skillFragment{}, fmt.Errorf("EMBEDDING_INVALID")
			}
		} else if key != "content" && key != "_search_text" {
			payload[key] = value
		}
	}
	if len(vector) == 0 || len(vector) > 32768 {
		return nil, skillFragment{}, fmt.Errorf("EMBEDDING_INVALID")
	}
	raw, e := json.Marshal(payload)
	if e != nil {
		return nil, skillFragment{}, e
	}
	text, _ := doc["_search_text"].(string)
	meta, e := splitSkillUTF8(string(raw), 8192)
	if e != nil {
		return nil, skillFragment{}, e
	}
	texts, e := splitSkillUTF8(text, 8192)
	if e != nil {
		return nil, skillFragment{}, e
	}
	hashInput, _ := json.Marshal([]any{payload, text, vector})
	sum := sha256.Sum256(hashInput)
	digest := fmt.Sprintf("%x", sum)
	group := fragmentID(id + "\x00" + digest)
	rows := []skillFragment{}
	for kind, parts := range map[string][]string{"meta": meta, "text": texts} {
		for i, part := range parts {
			row := skillFragment{id: fragmentID(id + "\x00" + digest + "\x00" + kind + "\x00" + strconv.Itoa(i)), doc: id, digest: digest, kind: kind, group: group, ordinal: int64(i), vector: vector}
			if kind == "meta" {
				row.payload = part
			} else {
				row.text = part
			}
			rows = append(rows, row)
		}
	}
	counts, _ := json.Marshal(map[string]int{"meta_chunks": len(meta), "text_chunks": len(texts)})
	head := skillFragment{id: fragmentID(id + "\x00head"), doc: id, digest: digest, kind: "head", group: group, payload: string(counts), vector: vector}
	return rows, head, nil
}

// Preparation failure must never invoke the single-row visibility publication.
func publishSkillDocument(rows []skillFragment, head skillFragment, write func([]skillFragment) error) error {
	if err := write(rows); err != nil {
		return err
	}
	return write([]skillFragment{head})
}

func (m *SkillMilvus) IndexDocument(ctx context.Context, name, id string, doc map[string]interface{}) error {
	rows, head, e := skillDocumentFragments(id, doc)
	if e != nil {
		return e
	}
	physical, e := m.resolve(ctx, name)
	if e != nil {
		return e
	}
	if e = publishSkillDocument(rows, head, func(batch []skillFragment) error { return m.writeFragments(ctx, physical, batch) }); e != nil {
		return e
	}
	// Validate an actual dense read after publication, not a fabricated vector.
	hits, e := m.Client.Search(ctx, mc.NewSearchOption(physical, 1, []me.Vector{me.FloatVector(head.vector)}).WithANNSField("vector").WithFilter("id == "+strconv.Quote(head.id)).WithOutputFields("id").WithConsistencyLevel(me.ClStrong))
	if e != nil {
		return e
	}
	if len(hits) != 1 || hits[0].Err != nil || hits[0].ResultCount != 1 {
		return fmt.Errorf("dense readback failed")
	}
	_, e = m.Client.Delete(ctx, mc.NewDeleteOption(physical).WithExpr("doc_id == "+strconv.Quote(id)+" && digest != "+strconv.Quote(head.digest)))
	return e
}
func (m *SkillMilvus) DeleteDocument(ctx context.Context, name, id string) error {
	exists, e := m.TableExists(ctx, name)
	if e != nil || !exists {
		return e
	}
	physical, e := m.resolve(ctx, name)
	if e != nil {
		return e
	}
	_, e = m.Client.Delete(ctx, mc.NewDeleteOption(physical).WithExpr("doc_id == "+strconv.Quote(id)))
	if e != nil {
		return e
	}
	result, e := m.Client.Query(ctx, mc.NewQueryOption(physical).WithFilter("doc_id == "+strconv.Quote(id)).WithOutputFields("count(*)").WithConsistencyLevel(me.ClStrong))
	if e != nil {
		return e
	}
	count, e := result.GetColumn("count(*)").Get(0)
	if e != nil {
		return e
	}
	if count != int64(0) {
		return fmt.Errorf("document fragments remain")
	}
	return nil
}
func (m *SkillMilvus) readMetadata(ctx context.Context, name, id, digest string, count int) (map[string]interface{}, error) {
	// A document has at most a 5MB SKILL.md metadata source; 8192-byte fragments
	// fit comfortably inside Milvus's query window. Full body is never metadata.
	result, e := m.Client.Query(ctx, mc.NewQueryOption(name).WithFilter("doc_id == "+strconv.Quote(id)+" && digest == "+strconv.Quote(digest)+" && kind == \"meta\"").WithOutputFields("ordinal", "payload").WithLimit(16384).WithConsistencyLevel(me.ClStrong))
	if e != nil {
		return nil, e
	}
	if count < 1 || result.ResultCount != count {
		return nil, fmt.Errorf("metadata fragment count mismatch")
	}
	parts := make([]string, result.ResultCount)
	seen := make(map[int64]bool)
	for i := 0; i < result.ResultCount; i++ {
		raw, e := result.GetColumn("ordinal").Get(i)
		if e != nil {
			return nil, e
		}
		ordinal, ok := raw.(int64)
		if !ok || ordinal < 0 || ordinal >= int64(len(parts)) || seen[ordinal] {
			return nil, fmt.Errorf("metadata fragment ordinal invalid")
		}
		seen[ordinal] = true
		parts[ordinal], e = resultString(&result, "payload", i)
		if e != nil {
			return nil, e
		}
	}
	var payload map[string]interface{}
	if e = json.Unmarshal([]byte(strings.Join(parts, "")), &payload); e != nil {
		return nil, e
	}
	return payload, nil
}
func (m *SkillMilvus) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	if len(req.IndexNames) != 1 {
		return nil, fmt.Errorf("one skill index required")
	}
	name, e := m.resolve(ctx, req.IndexNames[0])
	if e != nil {
		return nil, e
	}
	limit := req.Limit
	if limit < 1 {
		limit = 100
	}
	limit = min(limit, 16384)
	var text *types.MatchTextExpr
	var dense *types.MatchDenseExpr
	weight := 1.0
	for _, raw := range req.MatchExprs {
		switch x := raw.(type) {
		case *types.MatchTextExpr:
			text = x
		case *types.MatchDenseExpr:
			dense = x
		case *types.FusionExpr:
			if v, ok := x.FusionParams["weights"].(string); ok {
				parts := strings.Split(v, ",")
				if len(parts) == 2 {
					weight, _ = strconv.ParseFloat(parts[1], 64)
				}
			}
		}
	}
	if dense == nil && (text == nil || text.MatchingText == "") {
		return m.listDocuments(ctx, name, req)
	}
	type score struct {
		id, digest string
		kw, vec    float64
	}
	scores := map[string]*score{}
	add := func(result mc.ResultSet, isVector bool) error {
		if result.Err != nil {
			return result.Err
		}
		for i := 0; i < result.ResultCount; i++ {
			id, e := resultString(&result, "doc_id", i)
			if e != nil {
				return e
			}
			digest, e := resultString(&result, "digest", i)
			if e != nil {
				return e
			}
			key := id + "\x00" + digest
			h := scores[key]
			if h == nil {
				h = &score{id: id, digest: digest}
				scores[key] = h
			}
			value := float64(result.Scores[i])
			if isVector {
				h.vec = max(h.vec, max(0, min(1, value)))
			} else {
				h.kw = max(h.kw, value/(1+value))
			}
		}
		return nil
	}
	run := func(vector me.Vector, field, kind string, isVector bool) error {
		filters := []string{"kind == " + strconv.Quote(kind)}
		if !isVector {
			// Unpublished fragments cannot compete with visible documents.
			groups, err := m.activeGroups(ctx, name)
			if err != nil {
				return err
			}
			filters = nil
			for start := 0; start < len(groups); start += 256 {
				raw, _ := json.Marshal(groups[start:min(start+256, len(groups))])
				filters = append(filters, "kind == \"text\" && group_id in "+string(raw))
			}
		}
		for _, filter := range filters {
			option := mc.NewSearchOption(name, limit, []me.Vector{vector}).WithANNSField(field).WithFilter(filter).WithOutputFields("doc_id", "digest").WithConsistencyLevel(me.ClStrong)
			if !isVector {
				option = option.WithGroupByField("group_id").WithGroupSize(1)
			}
			results, e := m.Client.Search(ctx, option)
			if e != nil {
				return e
			}
			for _, result := range results {
				if e = add(result, isVector); e != nil {
					return e
				}
			}
		}
		return nil
	}
	if text != nil && text.MatchingText != "" && (dense == nil || weight < 1) {
		if e = run(me.Text(text.MatchingText), "sparse", "text", false); e != nil {
			return nil, e
		}
	}
	if dense != nil {
		v := make([]float32, len(dense.EmbeddingData))
		for i, f := range dense.EmbeddingData {
			v[i] = float32(f)
		}
		if e = run(me.FloatVector(v), "vector", "head", true); e != nil {
			return nil, e
		}
	} else {
		weight = 0
	}
	chunks := []map[string]interface{}{}
	for _, h := range scores {
		head, e := m.Client.Query(ctx, mc.NewQueryOption(name).WithFilter("id == "+strconv.Quote(fragmentID(h.id+"\x00head"))).WithOutputFields("digest", "payload").WithConsistencyLevel(me.ClStrong))
		if e != nil {
			return nil, e
		}
		if head.ResultCount != 1 {
			continue
		}
		current, e := resultString(&head, "digest", 0)
		if e != nil {
			return nil, e
		}
		if current != h.digest {
			continue
		}
		countsRaw, e := resultString(&head, "payload", 0)
		if e != nil {
			return nil, e
		}
		var counts map[string]int
		if e = json.Unmarshal([]byte(countsRaw), &counts); e != nil {
			return nil, e
		}
		payload, e := m.readMetadata(ctx, name, h.id, h.digest, counts["meta_chunks"])
		if e != nil {
			return nil, e
		}
		payload["_score"] = (1-weight)*h.kw + weight*h.vec
		payload["SCORE"] = h.kw
		payload["SIMILARITY"] = h.vec
		chunks = append(chunks, payload)
	}
	sort.Slice(chunks, func(i, j int) bool {
		a, b := chunks[i]["_score"].(float64), chunks[j]["_score"].(float64)
		if a == b {
			return fmt.Sprint(chunks[i]["skill_id"]) < fmt.Sprint(chunks[j]["skill_id"])
		}
		return a > b
	})
	total := int64(len(chunks))
	if len(chunks) > limit {
		chunks = chunks[:limit]
	}
	return &types.SearchResult{Chunks: chunks, Total: total}, nil
}

// List logical heads via an iterator, not a bounded vector candidate window.
func (m *SkillMilvus) listDocuments(ctx context.Context, name string, req *types.SearchRequest) (*types.SearchResult, error) {
	iterator, err := m.Client.QueryIterator(ctx, mc.NewQueryIteratorOption(name).WithFilter("kind == \"head\"").WithOutputFields("doc_id", "digest", "payload").WithBatchSize(1000))
	if err != nil {
		return nil, err
	}
	chunks := []map[string]interface{}{}
	for {
		batch, err := iterator.Next(ctx)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		for i := 0; i < batch.ResultCount; i++ {
			id, err := resultString(&batch, "doc_id", i)
			if err != nil {
				return nil, err
			}
			digest, err := resultString(&batch, "digest", i)
			if err != nil {
				return nil, err
			}
			raw, err := resultString(&batch, "payload", i)
			if err != nil {
				return nil, err
			}
			counts := map[string]int{}
			if err = json.Unmarshal([]byte(raw), &counts); err != nil {
				return nil, err
			}
			payload, err := m.readMetadata(ctx, name, id, digest, counts["meta_chunks"])
			if err != nil {
				return nil, err
			}
			payload["_score"] = float64(0)
			chunks = append(chunks, payload)
		}
	}
	sort.Slice(chunks, func(i, j int) bool {
		if req.OrderBy != nil {
			for _, field := range req.OrderBy.Fields {
				a, b := chunks[i][field.Field], chunks[j][field.Field]
				if fmt.Sprint(a) == fmt.Sprint(b) {
					continue
				}
				less := fmt.Sprint(a) < fmt.Sprint(b)
				if x, ok := a.(float64); ok {
					if y, ok := b.(float64); ok {
						less = x < y
					}
				}
				if field.Type == types.SortDesc {
					return !less
				}
				return less
			}
		}
		return fmt.Sprint(chunks[i]["skill_id"]) < fmt.Sprint(chunks[j]["skill_id"])
	})
	total := len(chunks)
	start := min(total, max(0, req.Offset))
	end := min(total, start+max(1, req.Limit))
	return &types.SearchResult{Chunks: chunks[start:end], Total: int64(total)}, nil
}

func (m *SkillMilvus) activeGroups(ctx context.Context, name string) ([]string, error) {
	iterator, err := m.Client.QueryIterator(ctx, mc.NewQueryIteratorOption(name).WithFilter("kind == \"head\"").WithOutputFields("group_id").WithBatchSize(1000))
	if err != nil {
		return nil, err
	}
	groups := []string{}
	for {
		batch, err := iterator.Next(ctx)
		if err == io.EOF {
			return groups, nil
		}
		if err != nil {
			return nil, err
		}
		for i := 0; i < batch.ResultCount; i++ {
			group, err := resultString(&batch, "group_id", i)
			if err != nil {
				return nil, err
			}
			groups = append(groups, group)
		}
	}
}
