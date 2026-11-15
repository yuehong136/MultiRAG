package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"mime/multipart"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine/elasticsearch"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/tokenizer"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSkillDirectorySelectionAndMetadata(t *testing.T) {
	s := NewSkillIndexerService()
	rows := []*entity.File{{Name: "1.9.0", Type: "folder"}, {Name: "2.0.0", Type: "folder"}, {Name: "3x.0.0", Type: "folder"}, {Name: "not-semver", Type: "folder"}}
	if got := s.findLatestVersion(rows); got == nil || got.Name != "2.0.0" {
		t.Fatalf("wrong highest version: %v", got)
	}
	name, description, tags := s.parseSkillMetadata("---\nname: orange\ndescription: |\n  multiline\n  description\ntags: [fruit]\n---\nbody", "fallback")
	if name != "orange" || description != "multiline\ndescription\n" || len(tags) != 1 || tags[0] != "fruit" {
		t.Fatalf("metadata %q %q %v", name, description, tags)
	}
}

func TestSkillNumericVersionDoesNotOverflowOrAcceptSigns(t *testing.T) {
	s := NewSkillIndexerService()
	rows := []*entity.File{{Name: "+999.0.0", Type: "folder"}, {Name: "2.0.0", Type: "folder"}, {Name: "99999999999999999999999999999999.0.0", Type: "folder"}}
	got := s.findLatestVersion(rows)
	if got == nil || got.Name != rows[2].Name {
		t.Fatal("numeric ordering changed", got)
	}
	if s.findLatestVersion(rows[:1]) != nil {
		t.Fatal("signed version accepted")
	}
	for _, name := range []string{"3x.0.0", "3.0.0suffix", "-3.0.0", "3.0", "3..0", "3.0.0.0"} {
		if s.findLatestVersion([]*entity.File{{Name: name, Type: "folder"}}) != nil {
			t.Errorf("accepted malformed version %q", name)
		}
	}
}

func TestCanonicalSkillUploadPathBoundarySQLObjects(t *testing.T) {
	f := scratch.New(t)
	oldDB, oldStore := dao.DB, storage.GetStorageFactory().GetStorage()
	dao.DB = f.DB
	storage.GetStorageFactory().SetStorage(f.Store)
	t.Cleanup(func() { dao.DB = oldDB; storage.GetStorageFactory().SetStorage(oldStore) })
	if err := f.DB.AutoMigrate(&entity.File{}); err != nil {
		t.Fatal(err)
	}
	owner, parent := f.Prefix+"owner", f.Prefix+"root"
	if err := f.DB.Create(&entity.File{ID: parent, ParentID: parent, TenantID: owner, CreatedBy: owner, Name: "/", Type: "folder"}).Error; err != nil {
		t.Fatal(err)
	}
	files := NewFileService()
	for _, name := range []string{strings.Repeat("a", 255), "短中文.txt"} {
		result, err := files.UploadFile(owner, parent, canonicalFileHeaders(t, name))
		if err != nil || len(result) != 1 || result[0]["name"] != name {
			t.Fatal("supported filename rejected", result, err)
		}
		f.ReadObject(t, parent, result[0]["location"].(string), []byte("actual uploaded contents"))
		var stored string
		if err := f.Read.QueryRow("SELECT name FROM file WHERE id=? AND parent_id=?", result[0]["id"], parent).Scan(&stored); err != nil || stored != name {
			t.Fatal("filename SQL readback", stored, err)
		}
	}
	before := len(f.ObjectKeys(t))
	// The frozen location is the filename. MinIO's actual component limit is
	// not the retired private validator's claim of 255 Unicode code points.
	for _, name := range []string{strings.Repeat("a", 256), strings.Repeat("中", 255)} {
		if result, err := files.UploadFile(owner, parent, canonicalFileHeaders(t, name)); err == nil || result != nil {
			t.Fatal("unsupported storage name reported successful upload", result, err)
		}
		if len(f.ObjectKeys(t)) != before {
			t.Fatal("unsupported storage name leaked attempted object")
		}
	}
	for _, name := range []string{"../bad", "/absolute", "a//b", "a/./b", "a\\b"} {
		beforeCalls := f.Store.Calls
		if result, err := files.UploadFile(owner, parent, canonicalFileHeaders(t, name)); err == nil || result != nil {
			t.Fatal("invalid skill source path accepted", name, result, err)
		}
		if beforeCalls != f.Store.Calls {
			t.Fatal("invalid path reached object storage", name)
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM file WHERE id<>?", parent).Scan(&count); err != nil || count != 2 {
		t.Fatal("rejected source upload changed SQL", count, err)
	}
	if len(f.ObjectKeys(t)) != before {
		t.Fatal("rejected source paths changed physical objects")
	}
	f.Journal.Event("canonical-skill-upload-path-readback", map[string]any{"ascii255": true, "ascii256_rejected_compensated": true, "short_unicode": true, "unicode255_storage_limit_rejected_without_partial_write": true, "invalid_paths_zero_object_io": true})
}

// This exercises the original Go spaces/File/Indexer/Search route. The model
// endpoint is controlled; SQL, object bytes and all index operations are real.
func TestCanonicalSkillsSQLObjectsAndElasticsearch(t *testing.T) {
	f := scratch.New(t)
	_, source, _, _ := runtime.Caller(0)
	t.Chdir(filepath.Join(filepath.Dir(source), "..", ".."))
	if err := server.Init(filepath.Join(filepath.Dir(os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")), "service_conf.yaml")); err != nil {
		t.Fatal("private Skills config initialization")
	}
	server.GetConfig().Database = f.Config.Database
	oldDB, oldStore := dao.DB, storage.GetStorageFactory().GetStorage()
	if err := dao.InitDB(); err != nil {
		t.Fatal(err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	storage.GetStorageFactory().SetStorage(f.Store)
	t.Cleanup(func() { pool.Close(); dao.DB = oldDB; storage.GetStorageFactory().SetStorage(oldStore) })
	if !tokenizer.IsInitialized() {
		if err := tokenizer.Init(&tokenizer.PoolConfig{DictPath: os.Getenv("RAGFLOW_DICT_PATH")}); err != nil {
			t.Fatal("initialize original Skills tokenizer", err)
		}
		t.Cleanup(func() { tokenizer.Close() })
	}
	owner, foreign := f.Prefix+"owner", f.Prefix+"foreign"
	active := "1"
	for _, tenant := range []string{owner, foreign} {
		if err := dao.DB.Create(&entity.Tenant{ID: tenant, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: tenant, UserID: tenant, TenantID: tenant, Role: "owner", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	var embeddingCalls atomic.Int64
	var failEmbedding atomic.Bool
	embeddingStarted, embeddingStopped, releaseEmbedding := make(chan struct{}, 1), make(chan struct{}, 1), make(chan struct{})
	defer close(releaseEmbedding)
	peer := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		embeddingCalls.Add(1)
		if r.Method != "POST" || !strings.HasSuffix(r.URL.Path, "/embeddings") || r.Header.Get("Authorization") != "Bearer skill-key" {
			t.Error("Skills model identity or endpoint lost")
		}
		if failEmbedding.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprint(w, "controlled embedding failure")
			return
		}
		var body struct {
			Model string   `json:"model"`
			Input []string `json:"input"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Model != "text-embedding-3-small" {
			t.Error("Skills model body changed", err)
			return
		}
		if len(body.Input) == 1 && body.Input[0] == "wait-for-cancel" {
			embeddingStarted <- struct{}{}
			select {
			case <-r.Context().Done():
				embeddingStopped <- struct{}{}
			case <-releaseEmbedding:
			}
			return
		}
		rows := make([]map[string]any, len(body.Input))
		for i, text := range body.Input {
			vector := []float64{1, 0}
			if strings.Contains(text, "mineral") || strings.Contains(text, "stone") {
				vector = []float64{0, 1}
			}
			rows[i] = map[string]any{"index": i, "embedding": vector}
		}
		json.NewEncoder(w).Encode(map[string]any{"data": rows})
	}))
	models := NewModelProviderService()
	if code, err := models.AddModelProvider("OpenAI", owner); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	if code, err := models.CreateProviderInstance("OpenAI", "skill", "skill-key", peer.URL, "default", owner); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	if code, err := models.AddModel(&AddModelRequest{ProviderName: "OpenAI", InstanceName: "skill", Models: []ModelRequest{{ModelName: "text-embedding-3-small", ModelTypes: []string{"embedding"}}}}, owner); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	embd := "text-embedding-3-small@skill@OpenAI"
	spaceService, indexer, search := NewSkillSpaceService(), NewSkillIndexerService(), NewSkillSearchService()
	space, code, err := spaceService.CreateSpace(&CreateSpaceRequest{TenantID: owner, Name: "orchard", EmbdID: embd})
	if err != nil || code != common.CodeSuccess {
		t.Fatal("create original space", space, code, err)
	}
	spaceID, folderID := space["id"].(string), space["folder_id"].(string)
	otherSpace, code, err := spaceService.CreateSpace(&CreateSpaceRequest{TenantID: foreign, Name: "orchard"})
	if err != nil || code != common.CodeSuccess {
		t.Fatal("foreign own space", code, err)
	}
	foreignSpaceID := otherSpace["id"].(string)
	var storedFolder string
	if err := f.Read.QueryRow("SELECT folder_id FROM skill_spaces WHERE id=? AND tenant_id=?", spaceID, owner).Scan(&storedFolder); err != nil || storedFolder != folderID {
		t.Fatal("independent space folder readback", storedFolder, err)
	}
	fileService := NewFileService()
	makeFolder := func(tenant, parent, name string) string {
		t.Helper()
		row, err := fileService.CreateFolder(tenant, name, parent, "folder")
		if err != nil {
			t.Fatal("create Skills directory", err)
		}
		return row["id"].(string)
	}
	putFile := func(tenant, parent, name, content string) string {
		t.Helper()
		var body bytes.Buffer
		writer := multipart.NewWriter(&body)
		part, err := writer.CreateFormFile("file", name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := part.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
		if err := writer.Close(); err != nil {
			t.Fatal(err)
		}
		form, err := multipart.NewReader(&body, writer.Boundary()).ReadForm(1 << 20)
		if err != nil {
			t.Fatal(err)
		}
		defer form.RemoveAll()
		rows, err := fileService.UploadFile(tenant, parent, form.File["file"])
		if err != nil || len(rows) != 1 {
			t.Fatal("upload Skills source", rows, err)
		}
		f.ReadObject(t, parent, rows[0]["location"].(string), []byte(content))
		return rows[0]["location"].(string)
	}
	citrus := makeFolder(owner, folderID, "citrus")
	oldVersion := makeFolder(owner, citrus, "1.0.0")
	latest := makeFolder(owner, citrus, "2.0.0")
	invalidVersion := makeFolder(owner, citrus, "999x.0.0")
	putFile(owner, oldVersion, "SKILL.md", "---\nname: stale\n---\nstale version")
	putFile(owner, invalidVersion, "SKILL.md", "---\nname: malformed\n---\ninvalid version")
	latestContent := "---\nname: citrus\ndescription: orange semantic\ntags: [fruit]\n---\nOrange latest body"
	latestKey := putFile(owner, latest, "SKILL.md", latestContent)
	putFile(owner, latest, "notes.txt", "latest attachment text")
	putFile(owner, latest, "image.png", "ignored binary object")
	mineral := makeFolder(owner, folderID, "mineral")
	mineralVersion := makeFolder(owner, mineral, "1.0.0")
	putFile(owner, mineralVersion, "SKILL.md", "---\nname: mineral\ndescription: stone guide\n---\nstone body")
	foreignSkill := makeFolder(foreign, otherSpace["folder_id"].(string), "foreign")
	foreignVersion := makeFolder(foreign, foreignSkill, "1.0.0")
	foreignKey := putFile(foreign, foreignVersion, "SKILL.md", "---\nname: foreign\n---\nforeign body")
	fieldConfig := entity.DefaultFieldConfig()
	fieldConfig.Content.Enabled = true
	if _, code, err := search.UpdateConfig(&UpdateConfigRequest{TenantID: owner, SpaceID: spaceID, EmbdID: embd, VectorSimilarityWeight: 0, SimilarityThreshold: 0, FieldConfig: fieldConfig, TopK: 10}); err != nil || code != common.CodeSuccess {
		t.Fatal("persist Skills config", code, err)
	}
	actualURL, err := url.Parse(f.Config.Elasticsearch.Hosts)
	if err != nil {
		t.Fatal(err)
	}
	reverse := httputil.NewSingleHostReverseProxy(actualURL)
	var engineCalls atomic.Int64
	var failDrop atomic.Bool
	var failExists atomic.Bool
	indexName := getSkillIndexName(owner, spaceID)
	proxy := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		engineCalls.Add(1)
		if failExists.Load() && r.Method == "HEAD" && r.URL.Path == "/"+indexName {
			w.Header().Set("X-Elastic-Product", "Elasticsearch")
			w.WriteHeader(503)
			return
		}
		if failDrop.Load() && r.Method == "DELETE" && r.URL.Path == "/"+indexName {
			w.Header().Set("X-Elastic-Product", "Elasticsearch")
			w.WriteHeader(503)
			fmt.Fprint(w, `{"error":"controlled index deletion failure"}`)
			return
		}
		reverse.ServeHTTP(w, r)
	}))
	engineConfig := f.Config.Elasticsearch
	engineConfig.Hosts = proxy.URL
	index, err := elasticsearch.NewEngine(&engineConfig)
	if err != nil {
		t.Fatal("connect original Skills Elasticsearch", err)
	}
	f.Journal.Event("reserve-skill-index", map[string]any{"index": indexName})
	t.Cleanup(func() {
		failDrop.Store(false)
		failExists.Store(false)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		reader, err := elasticsearch.NewEngine(&f.Config.Elasticsearch)
		if err != nil {
			t.Error("independent Skills cleanup client", err)
			return
		}
		exists, err := reader.ChunkStoreExists(ctx, indexName, "skill")
		if err != nil {
			t.Error("inspect Skills cleanup index", err)
			return
		}
		if exists {
			if err := reader.DropChunkStore(ctx, indexName, "skill"); err != nil {
				t.Error("drop Skills index", err)
			}
		}
		exists, err = reader.ChunkStoreExists(ctx, indexName, "skill")
		f.Journal.Event("cleanup-skill-index-readback", map[string]any{"index": indexName, "absent": err == nil && !exists})
		if err != nil || exists {
			t.Error("Skills index survived cleanup", err)
		}
	})
	result, err := indexer.ReindexAll(t.Context(), owner, spaceID, index, embd)
	if err != nil || result["indexed_count"] != 2 {
		t.Fatal("original Skills reindex", result, err)
	}
	reader, err := elasticsearch.NewEngine(&f.Config.Elasticsearch)
	if err != nil {
		t.Fatal("independent Skills readback", err)
	}
	readIndex := func() []map[string]interface{} {
		t.Helper()
		out, err := reader.Search(t.Context(), &types.SearchRequest{IndexNames: []string{indexName}, Limit: 20})
		if err != nil {
			t.Fatal("independent Skills indexed rows", err)
		}
		return out.Chunks
	}
	rows := readIndex()
	if len(rows) != 2 {
		t.Fatal("indexed Skills row count", rows)
	}
	for _, row := range rows {
		if row["name"] == "citrus" && (row["folder_id"] != citrus || row["version"] != "2.0.0" || !strings.Contains(fmt.Sprint(row["content"]), "latest attachment text") || strings.Contains(fmt.Sprint(row["content"]), "ignored binary") || strings.Contains(fmt.Sprint(row["content"]), "stale version")) {
			t.Fatal("latest version/metadata/attachment binding", row)
		}
	}
	for _, mode := range []struct {
		name   string
		weight float64
	}{{"keyword", 0}, {"vector", 1}, {"hybrid", 0.5}} {
		threshold := 0.7
		if mode.weight == 0 {
			threshold = 0
		}
		if _, code, err := search.UpdateConfig(&UpdateConfigRequest{TenantID: owner, SpaceID: spaceID, EmbdID: embd, VectorSimilarityWeight: mode.weight, SimilarityThreshold: threshold, FieldConfig: fieldConfig, TopK: 10}); err != nil || code != common.CodeSuccess {
			t.Fatal(code, err)
		}
		before := embeddingCalls.Load()
		query := "orange"
		if mode.weight == 1 {
			query = "unspoken"
		}
		out, code, err := search.Search(t.Context(), &SearchRequest{TenantID: owner, SpaceID: spaceID, Query: query}, index)
		if err != nil || code != common.CodeSuccess || out.SearchType != mode.name || len(out.Skills) != 1 || out.Skills[0].Name != "citrus" || out.Skills[0].FolderID != citrus || out.Skills[0].Version != "2.0.0" {
			t.Fatal("actual Skills search", mode.name, out, code, err, readIndex())
		}
		if mode.weight == 0 && embeddingCalls.Load() != before {
			t.Fatal("keyword called embedding")
		}
		if mode.weight > 0 && embeddingCalls.Load() <= before {
			t.Fatal("vector/hybrid did not call actual model")
		}
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	for _, operation := range []struct {
		name string
		run  func() error
	}{
		{"query", func() error { _, err := search.getEmbedding(ctx, "cancelled", embd, owner); return err }},
		{"single", func() error { _, err := indexer.generateEmbedding(ctx, "cancelled", embd, owner); return err }},
		{"batch", func() error {
			_, err := indexer.generateEmbeddings(ctx, []string{"cancelled"}, embd, owner)
			return err
		}},
		{"dimension", func() error { _, err := indexer.getEmbeddingDimension(ctx, owner, embd); return err }},
	} {
		before := embeddingCalls.Load()
		if err := operation.run(); !errors.Is(err, context.Canceled) || before != embeddingCalls.Load() {
			t.Fatal("cancelled Skills model operation reached supplier or lost cause", operation.name, err, before, embeddingCalls.Load())
		}
	}
	inflight, stopInflight := context.WithCancel(t.Context())
	returned := make(chan error, 1)
	go func() {
		_, _, err := search.Search(inflight, &SearchRequest{TenantID: owner, SpaceID: spaceID, Query: "wait-for-cancel"}, index)
		returned <- err
	}()
	select {
	case <-embeddingStarted:
	case <-time.After(3 * time.Second):
		stopInflight()
		t.Fatal("actual Skills search did not start supplier request")
	}
	stopInflight()
	select {
	case err := <-returned:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("Skills supplier cancellation cause lost", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Skills supplier request ignored cancellation")
	}
	select {
	case <-embeddingStopped:
	case <-time.After(3 * time.Second):
		t.Fatal("Skills supplier connection did not observe cancellation")
	}
	// A tenant-supplied foreign space ID must never mutate that space or create
	// a foreign-space config before the ownership rejection.
	beforeEngine, beforeModel, beforeObjects := engineCalls.Load(), embeddingCalls.Load(), f.Store.Calls
	var configCount int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM skill_search_configs").Scan(&configCount); err != nil {
		t.Fatal(err)
	}
	if got, _, err := spaceService.GetSpace(spaceID, foreign); err == nil || got != nil {
		t.Fatal("foreign Skills get", got, err)
	}
	if got, _, err := spaceService.UpdateSpace(spaceID, foreign, &UpdateSpaceRequest{Name: "stolen"}); err == nil || got != nil {
		t.Fatal("foreign Skills update", got, err)
	}
	if _, err := spaceService.DeleteSpace(spaceID, foreign, index, t.Context()); err == nil {
		t.Fatal("foreign Skills delete")
	}
	if got, err := indexer.ReindexAll(t.Context(), foreign, spaceID, index, embd); err == nil || got != nil {
		t.Fatal("foreign Skills reindex", got, err)
	}
	var afterConfig int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM skill_search_configs").Scan(&afterConfig); err != nil {
		t.Fatal(err)
	}
	if beforeEngine != engineCalls.Load() || beforeModel != embeddingCalls.Load() || beforeObjects != f.Store.Calls || configCount != afterConfig {
		t.Fatal("foreign Skills refusal caused effects", beforeEngine, engineCalls.Load(), beforeModel, embeddingCalls.Load(), configCount, afterConfig)
	}
	if got, code, err := search.Search(t.Context(), &SearchRequest{TenantID: foreign, SpaceID: spaceID, Query: "orange"}, index); err != nil || code != common.CodeSuccess || got.Total != 0 {
		t.Fatal("foreign Skills search leaked", got, code, err)
	}
	if got, code, err := spaceService.GetSpace(foreignSpaceID, foreign); err != nil || code != common.CodeSuccess || got["name"] != "orchard" {
		t.Fatal("foreign owned space changed", got, err)
	}
	// The optional text-only fallback is the frozen IndexSkill contract. A
	// dimension probe failure still must return an error without fake indexing.
	failEmbedding.Store(true)
	beforeRows := len(readIndex())
	if err := indexer.IndexSkill(t.Context(), owner, spaceID, SkillInfo{ID: "must-not-exist", Name: "must-not-exist", Content: "body"}, index, embd); err == nil {
		t.Fatal("failed embedding dimension reported successful indexing")
	}
	failEmbedding.Store(false)
	if len(readIndex()) != beforeRows {
		t.Fatal("failed dimension changed index")
	}
	// A real MinIO read failure is not an empty skill that can replace the
	// previous good index and claim successful reindexing.
	if err := f.Store.Remove(latest, latestKey); err != nil {
		t.Fatal("remove exact owned source object for failure", err)
	}
	if got, err := indexer.ReindexAll(t.Context(), owner, spaceID, index, embd); err == nil || got != nil {
		t.Fatal("source read failure claimed successful reindex", got, err)
	}
	if err := f.Store.Put(latest, latestKey, []byte(latestContent)); err != nil {
		t.Fatal("restore exact owned source object", err)
	}
	if len(readIndex()) != beforeRows {
		t.Fatal("failed source read destroyed last searchable index")
	}
	// Do not mark deletion complete, remove SQL files or objects when the
	// actual Elasticsearch drop failed. A retry then removes all owned state.
	failDrop.Store(true)
	if code, err := spaceService.DeleteSpace(spaceID, owner, index, t.Context()); err != nil || code != common.CodeSuccess {
		t.Fatal("start original asynchronous delete", code, err)
	}
	waitStatus := func(want string) {
		t.Helper()
		deadline := time.Now().Add(15 * time.Second)
		for {
			var status string
			if err := f.Read.QueryRow("SELECT status FROM skill_spaces WHERE id=?", spaceID).Scan(&status); err != nil {
				t.Fatal(err)
			}
			if status == want {
				return
			}
			if status != entity.SpaceStatusDeleting || time.Now().After(deadline) {
				t.Fatal("Skills deletion state", status, "want", want)
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	waitStatus(entity.SpaceStatusActive)
	var remaining int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM file WHERE id=?", folderID).Scan(&remaining); err != nil || remaining != 1 {
		t.Fatal("failed delete removed source directory", remaining, err)
	}
	if len(readIndex()) != beforeRows {
		t.Fatal("failed drop removed indexed Skills")
	}
	failDrop.Store(false)
	failExists.Store(true)
	if code, err := spaceService.DeleteSpace(spaceID, owner, index, t.Context()); err != nil || code != common.CodeSuccess {
		t.Fatal("start deletion with unavailable index inspection", code, err)
	}
	waitStatus(entity.SpaceStatusActive)
	failExists.Store(false)
	if len(readIndex()) != beforeRows {
		t.Fatal("unknown index availability was treated as absent")
	}
	// The object really is removed before the controlled transport error. SQL
	// must retain its address, and a later retry handles the known absent index.
	removeFailure := errors.New("controlled response lost after object deletion")
	storage.GetStorageFactory().SetStorage(&canonicalUploadFailureStore{Storage: f.Store, removeErr: removeFailure})
	var sourceFileID string
	if err := f.Read.QueryRow("SELECT id FROM file WHERE parent_id=? AND location=?", latest, latestKey).Scan(&sourceFileID); err != nil {
		t.Fatal(err)
	}
	var removalPanic any
	var removed bool
	var removeMessage string
	func() {
		defer func() { removalPanic = recover() }()
		removed, removeMessage = fileService.DeleteFiles(t.Context(), owner, []string{sourceFileID})
	}()
	if removalPanic != nil || removed || !strings.Contains(removeMessage, removeFailure.Error()) {
		t.Fatal("object failure was not returned truthfully", removed, removeMessage, removalPanic)
	}
	var savedLocation string
	if err := f.Read.QueryRow("SELECT location FROM file WHERE id=?", sourceFileID).Scan(&savedLocation); err != nil || savedLocation != latestKey {
		t.Fatal("failed object deletion lost SQL address", savedLocation, err)
	}
	if err := f.Store.Put(latest, latestKey, []byte(latestContent)); err != nil {
		t.Fatal("restore removed object before asynchronous retry case", err)
	}
	if code, err := spaceService.DeleteSpace(spaceID, owner, index, t.Context()); err != nil || code != common.CodeSuccess {
		t.Fatal("start deletion with object failure", code, err)
	}
	waitStatus(entity.SpaceStatusActive)
	storage.GetStorageFactory().SetStorage(f.Store)
	if exists, err := reader.ChunkStoreExists(t.Context(), indexName, "skill"); err != nil || exists {
		t.Fatal("object failure occurred before actual index deletion", exists, err)
	}
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM file WHERE tenant_id=? AND location IS NOT NULL AND location<>''", owner).Scan(&remaining); err != nil || remaining == 0 {
		t.Fatal("object deletion failure lost SQL recovery addresses", remaining, err)
	}
	if code, err := spaceService.DeleteSpace(spaceID, owner, index, t.Context()); err != nil || code != common.CodeSuccess {
		t.Fatal("retry original deletion", code, err)
	}
	waitStatus(entity.SpaceStatusDeleted)
	if exists, err := reader.ChunkStoreExists(t.Context(), indexName, "skill"); err != nil || exists {
		t.Fatal("deleted Skills index readback", exists, err)
	}
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM file WHERE tenant_id=? AND source_type<>'system' AND parent_id<>id", owner).Scan(&remaining); err != nil || remaining != 0 {
		t.Fatal("deleted Skills source rows remain", remaining, err)
	}
	if keys := f.ObjectKeys(t); len(keys) != 1 {
		t.Fatal("owned Skills object cleanup affected foreign or leaked", keys)
	}
	f.ReadObject(t, foreignVersion, foreignKey, []byte("---\nname: foreign\n---\nforeign body"))
	f.Journal.Event("canonical-skills-closure", map[string]any{"mysql": true, "minio": true, "elasticsearch": true, "native_model_http": true, "highest_semver": "2.0.0", "keyword_vector_hybrid": true, "foreign_zero_mutation": true, "source_failure_preserves_index": true, "delete_failure_retry": true, "object_failure_retains_sql_address": true, "absent_index_retry": true, "unknown_index_does_not_skip": true})
}
