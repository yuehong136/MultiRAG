package handler_test

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine/elasticsearch"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/service/nlp"
	"multirag/internal/testsupport"
	"multirag/internal/tokenizer"
	"multirag/internal/utility"
)

type after10bbeFactory struct{ calls atomic.Int64 }

func (f *after10bbeFactory) CreateModelDriver(name string, urls map[string]string, suffix models.URLSuffix) (models.ModelDriver, error) {
	f.calls.Add(1)
	return models.NewModelFactory().CreateModelDriver(name, urls, suffix)
}

func after10bbeServer(t *testing.T, j *testsupport.CaseJournal, role string, h http.Handler) string {
	t.Helper()
	var listener net.Listener
	address := ""
	for i := 0; i < 30; i++ {
		id := uuid.New()
		address = fmt.Sprintf("127.0.0.1:%d", 20000+(int(id[0])<<8|int(id[1]))%30000)
		j.Event("reserve-listener", map[string]any{"role": role, "address": address})
		var err error
		listener, err = net.Listen("tcp4", address)
		if err == nil {
			break
		}
		j.Event("listener-bind-failed", map[string]any{"role": role, "address": address})
	}
	if listener == nil {
		t.Fatal("owned listener unavailable")
	}
	api := &http.Server{Handler: h, ReadHeaderTimeout: time.Second}
	done := make(chan error, 1)
	go func() { done <- api.Serve(listener) }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		err := api.Shutdown(ctx)
		if err != nil {
			_ = api.Close()
		}
		serveErr := <-done
		probe, probeErr := net.DialTimeout("tcp4", address, time.Second)
		if probe != nil {
			_ = probe.Close()
		}
		j.Event("cleanup-listener", map[string]any{"role": role, "address": address, "absent": probeErr != nil, "shutdown_ok": err == nil, "serve_closed": errors.Is(serveErr, http.ErrServerClosed)})
		if err != nil || probeErr == nil || !errors.Is(serveErr, http.ErrServerClosed) {
			t.Error("owned listener failed cleanup")
		}
	})
	return "http://" + address
}

// TestAfter10bbeAskChatRAGJWTSQL drives the production Router, JWT middleware,
// real SQL authorization, native query/prompt tokenization, ES adapter, and real
// embedding/rerank/chat HTTP drivers. Only the external provider/store peers are
// controlled loopback fixtures; it never uses a vendor account or business DB.
func TestAfter10bbeAskChatRAGJWTSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_AFTER10BBE_S_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned scratch admin config required")
		}
		t.Skip("owned scratch admin config required")
	}
	j := testsupport.NewCase(t)
	var cfg struct {
		AdminDSN string `json:"admin_dsn"`
		Endpoint string `json:"pg_endpoint"`
	}
	raw, err := os.ReadFile(config)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private scratch config unavailable")
	}
	parsed, err := url.Parse(cfg.AdminDSN)
	if err != nil {
		t.Fatal("invalid admin DSN")
	}
	port, portErr := strconv.Atoi(parsed.Port())
	if err != nil || parsed.Hostname() != "127.0.0.1" || portErr != nil || port < 1 || port > 65535 || parsed.Path != "/postgres" || cfg.Endpoint != parsed.Host || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") {
		t.Fatal("owned TCP SQL endpoint required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("open admin connection")
	}
	t.Cleanup(func() { _ = admin.Close() })
	database := "multirag_after10bbe_s_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	j.Event("reserve-database", map[string]any{"endpoint": cfg.Endpoint, "database": database})
	present := false
	if err := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", database).Scan(&present); err != nil || present {
		t.Fatal("scratch absence precondition")
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE IF EXISTS "` + database + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", database).Scan(&present)
		j.Event("cleanup-database", map[string]any{"endpoint": cfg.Endpoint, "database": database, "absent": readErr == nil && !present, "drop_ok": dropErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("scratch database cleanup failed")
		}
	})
	if _, err := admin.Exec(`CREATE DATABASE "` + database + `"`); err != nil {
		t.Fatal("create scratch DB")
	}
	parsed.Path = "/" + database
	db, err := gorm.Open(postgres.Open(parsed.String()), &gorm.Config{Logger: logger.Discard})
	if err != nil {
		t.Fatal("open scratch DB")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	reader, err := sql.Open("pgx", parsed.String())
	if err != nil {
		t.Fatal("open independent SQL reader")
	}
	t.Cleanup(func() { _ = reader.Close() })
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	tables := []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_search", "t_ai_knowledgebases", "t_ai_documents", "t_ai_tenant_llms", "t_ai_dialogs", "t_ai_conversations", "t_ai_api_tokens", "tenant_model_provider", "tenant_model_instance", "tenant_model"}
	j.Event("reserve-schema-tables", map[string]any{"database": database, "schema": "public", "tables": tables})
	if err := db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Search{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.TenantLLM{}, &entity.Chat{}, &entity.ChatSession{}, &entity.APIToken{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal("scratch schema migration", err)
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	// Reserve the private signing configuration before creation, including its hash.
	secret := uuid.NewString() + uuid.NewString()
	signing := filepath.Join(os.Getenv("TMPDIR"), "s-signing-"+uuid.NewString()+".yaml")
	signingRaw := []byte("server:\n  secret_key: " + secret + "\n")
	signingHash := sha256.Sum256(signingRaw)
	j.Event("reserve-signing-config", map[string]any{"path": signing, "sha256": hex.EncodeToString(signingHash[:])})
	if err := os.WriteFile(signing, signingRaw, 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		err := os.Remove(signing)
		_, statErr := os.Stat(signing)
		j.Event("cleanup-signing-config", map[string]any{"path": signing, "absent": os.IsNotExist(statErr)})
		if err != nil || !os.IsNotExist(statErr) {
			t.Error("signing config cleanup")
		}
	})
	oldKey := server.GetSecretKey()
	t.Cleanup(func() { server.SetSecretKey(oldKey) })
	if err := server.FromConfigFile(signing); err != nil {
		t.Fatal("load signing config")
	}
	server.SetSecretKey("")
	if err := server.InitVariables(nil); err != nil || server.GetSecretKey() != secret {
		t.Fatal("initialize owned signing key")
	}
	oldAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned S acceptance")
	t.Cleanup(func() { local.SetAdminStatus(oldAdmin.Status, oldAdmin.Reason) })
	if err := tokenizer.Init(&tokenizer.PoolConfig{DictPath: os.Getenv("RAG_DICT_PATH"), MinSize: 1, MaxSize: 2}); err != nil {
		t.Fatal("native tokenizer", err)
	}
	t.Cleanup(tokenizer.Close)
	if err := nlp.InitQueryBuilderFromTokenizer(os.Getenv("RAG_DICT_PATH")); err != nil {
		t.Fatal("query builder", err)
	}
	if n, err := service.NumTokensFromString("hello 世界"); err != nil || n < 1 {
		t.Fatal("native token count", n, err)
	}
	huge := []service.SourcedChunk{{ID: "oversized", Content: strings.Repeat("bounded content ", 100)}}
	if prompt, selected, err := service.BuildKnowledgePrompt(huge, 1); err != nil || prompt != "" || len(selected) != 0 {
		t.Fatal("native budget admitted partial oversized knowledge", err)
	}
	catalog, err := models.NewProviderManager("configs/models")
	if err != nil {
		t.Fatal(err)
	}

	var embedCalls, rerankCalls, chatCalls, searchCalls, hydrateCalls, metaCalls, pits atomic.Int64
	var mode atomic.Value
	mode.Store("success")
	cancelled := make(chan struct{}, 4)
	modelBase := after10bbeServer(t, j, "controlled-embedding-rerank-chat", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		current := mode.Load().(string)
		var body map[string]interface{}
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("decode provider request")
			w.WriteHeader(400)
			return
		}
		switch r.URL.Path {
		case "/embeddings":
			embedCalls.Add(1)
			if r.Header.Get("Authorization") != "Bearer s-embed-key" || body["model"] != "s-embedding" {
				t.Error("embedding binding changed")
				w.WriteHeader(400)
				return
			}
			input, ok := body["input"].([]interface{})
			if !ok {
				t.Error("embedding batch missing")
				w.WriteHeader(400)
				return
			}
			data := []map[string]interface{}{}
			for i, text := range input {
				if strings.Contains(fmt.Sprint(text), "private") {
					t.Error("reasoning entered citation embedder")
				}
				if current == "citation-failure" && strings.Contains(fmt.Sprint(text), "Grounded fact") {
					w.WriteHeader(400)
					fmt.Fprint(w, `{"error":"controlled citation failure"}`)
					return
				}
				data = append(data, map[string]interface{}{"index": i, "embedding": []float64{1, 0}})
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"data": data})
		case "/rerank":
			rerankCalls.Add(1)
			if r.Header.Get("Authorization") != "Bearer s-rerank-key" || body["model"] != "s-reranker" {
				t.Error("rerank owner/ID binding changed")
				w.WriteHeader(400)
				return
			}
			if current == "rerank-failure" {
				w.WriteHeader(400)
				fmt.Fprint(w, `{"error":"controlled rerank failure"}`)
				return
			}
			fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":0.99}],"usage":{"total_tokens":1}}`)
		case "/chat/completions":
			chatCalls.Add(1)
			if r.Header.Get("Authorization") != "Bearer s-chat-key" || body["model"] != "s-chat" {
				t.Error("chat owner/numeric binding changed")
				w.WriteHeader(400)
				return
			}
			msgs, _ := body["messages"].([]interface{})
			if len(msgs) < 2 {
				t.Error("chat history missing")
				w.WriteHeader(400)
				return
			}
			system := fmt.Sprint(msgs[0].(map[string]interface{})["content"])
			validSystem := strings.Contains(system, "Grounded manual passage") && strings.Contains(system, "s-chunk") && !strings.Contains(system, "{knowledge}")
			if current == "solo" {
				validSystem = system == "Solo rules" && len(msgs) == 3 && msgs[1].(map[string]interface{})["content"] == "prologue"
			}
			if !validSystem {
				t.Error("retrieved knowledge not injected", system)
				w.WriteHeader(400)
				return
			}
			if current == "image-history" {
				last, _ := msgs[len(msgs)-1].(map[string]interface{})
				blocks, ok := last["content"].([]interface{})
				if len(msgs) != 4 || msgs[2].(map[string]interface{})["content"] != "previous answer" || !ok || len(blocks) != 2 || blocks[1].(map[string]interface{})["type"] != "image_url" {
					t.Error("provider lost multimodal history")
				}
			}
			if body["temperature"] != float64(0) || body["top_p"] != float64(0) {
				t.Error("explicit zero generation settings lost")
			}
			if current == "provider-failure" {
				w.WriteHeader(400)
				fmt.Fprint(w, `{"error":"private provider failure"}`)
				return
			}
			if body["stream"] == true {
				w.Header().Set("Content-Type", "text/event-stream")
				write := func(content, reasoning string) {
					payload, _ := json.Marshal(map[string]interface{}{"choices": []interface{}{map[string]interface{}{"delta": map[string]interface{}{"content": content, "reasoning_content": reasoning}}}})
					fmt.Fprintf(w, "data: %s\n\n", payload)
					w.(http.Flusher).Flush()
				}
				write("<thi", "typed private ")
				write("nk>inline private</th", "")
				if current == "cancel" {
					write("ink>Partial visible", "")
					<-r.Context().Done()
					cancelled <- struct{}{}
					return
				}
				if current == "no-answer" {
					write("ink>", "")
				} else {
					if current == "configured-no-answer" {
						write("ink>No matching knowledge. [ID:s-chunk]", "")
					} else {
						write("ink>Grounded fact from manual.", "")
					}
				}
				fmt.Fprint(w, "data: [DONE]\n\n")
			} else {
				fmt.Fprint(w, `{"choices":[{"message":{"content":"<think>inline private</think>Grounded fact from manual.","reasoning_content":"typed private "}}]}`)
			}
		default:
			t.Error("unexpected provider endpoint", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	j.Event("reserve-controlled-index-identities", map[string]any{"indices": []string{"multirag_s-owner", "multirag_doc_meta_s-owner"}, "pit": "s-metadata-pit", "chunk_ids": []string{"s-chunk"}, "document_ids": []string{"s-doc", "s-other-doc"}})
	engineBase := after10bbeServer(t, j, "controlled-ES-adapter-peer", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Elastic-Product", "Elasticsearch")
		if r.URL.Path == "/" {
			fmt.Fprint(w, `{"version":{"number":"8.17.0"}}`)
			return
		}
		if strings.HasSuffix(r.URL.Path, "/_pit") && r.Method == "POST" {
			pits.Add(1)
			fmt.Fprint(w, `{"id":"s-metadata-pit"}`)
			return
		}
		if r.URL.Path == "/_pit" {
			pits.Add(-1)
			fmt.Fprint(w, `{"succeeded":true}`)
			return
		}
		data, _ := io.ReadAll(r.Body)
		text := string(data)
		if r.URL.Path == "/_search" {
			metaCalls.Add(1)
			if mode.Load() == "filter-failure" {
				w.WriteHeader(400)
				fmt.Fprint(w, `{"error":"controlled metadata failure"}`)
				return
			}
			if !strings.Contains(text, "s-kb") || !strings.Contains(text, "s-metadata-pit") {
				t.Error("metadata snapshot scope lost", text)
			}
			fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"metadata-physical","_source":{"id":"s-doc","kb_id":"s-kb","meta_fields":{"approved":false,"revision":9007199254740993,"zero":0}},"sort":[1]}]}}`)
			return
		}
		if r.URL.Path != "/multirag_s-owner/_search" {
			t.Error("cross-tenant search", r.URL.Path)
			w.WriteHeader(400)
			return
		}
		if !strings.Contains(text, "s-kb") {
			t.Error("KB scope lost", text)
		}
		if !strings.Contains(text, "content_with_weight") {
			hydrateCalls.Add(1)
			if !strings.Contains(text, "s-chunk") || !strings.Contains(text, "s-doc") || !strings.Contains(text, "q_2_vec") {
				t.Error("citation selection scope lost", text)
			}
			fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"vector-physical","_source":{"id":"s-chunk","kb_id":"s-kb","doc_id":"s-doc","q_2_vec":[1,0]}}]}}`)
			return
		}
		searchCalls.Add(1)
		if mode.Load() == "retrieval-failure" {
			w.WriteHeader(400)
			fmt.Fprint(w, `{"error":"controlled search failure"}`)
			return
		}
		fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"source-physical","_source":{"id":"s-chunk","kb_id":"s-kb","doc_id":"s-doc","docnm_kwd":"Report, Q1.pdf","content_with_weight":"Grounded manual passage.","content_ltks":"grounded manual passage","q_2_vec":[1,0],"available_int":1,"position_int":[[1,2,3,4,5]]},"highlight":{"content_with_weight":["<em>Grounded</em> manual passage."]}}]}}`)
	}))
	store, err := elasticsearch.NewEngine(&server.ElasticsearchConfig{Hosts: engineBase})
	if err != nil {
		t.Fatal("construct real ES adapter", err)
	}
	factory := &after10bbeFactory{}
	provider := service.NewModelProviderServiceWithCatalog(catalog, factory)
	chunks := service.NewChunkServiceWithModels(provider, store)
	ask := service.NewAskService(chunks, provider)
	chat := service.NewChatSessionServiceWithModels(provider, chunks)

	active, inactive := "1", "0"
	actors := []string{"s-owner", "s-member", "s-foreign", "s-inactive", "s-invite", "s-other-member"}
	j.Event("reserve-sql-identities", map[string]any{"database": database, "actors": actors, "searches": []string{"s-search", "s-deleted", "s-foreign-search", "s-invalid-model", "s-empty-search"}, "KBs": []string{"s-kb", "s-private", "s-other-kb"}, "legacyIDs": []int64{9007199254740993, 9007199254740995, 9007199254740997, 9007199254740999}, "dialogs": []string{"s-dialog", "s-foreign-dialog"}})
	create := func(t *testing.T, value interface{}) {
		t.Helper()
		if err := db.Create(value).Error; err != nil {
			t.Fatalf("seed scratch fixture %T: %v", value, err)
		}
	}
	tokens := map[string]string{}
	for _, actor := range actors {
		status := active
		if actor == "s-inactive" {
			status = inactive
		}
		create(t, &entity.User{ID: actor, Email: actor + "@after10bbe.invalid", Nickname: actor, Status: &status, IsActive: true, IsAuthenticated: true})
		tokens[actor], err = utility.SignUserJWT(actor+"@after10bbe.invalid", secret)
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, owner := range []string{"s-owner", "s-foreign"} {
		create(t, &entity.Tenant{ID: owner, Status: &active})
		create(t, &entity.UserTenant{ID: owner, UserID: owner, TenantID: owner, Role: "owner", Status: &active})
	}
	for _, actor := range []string{"s-member", "s-inactive", "s-invite", "s-other-member"} {
		role := "normal"
		if actor == "s-invite" {
			role = "invite"
		}
		create(t, &entity.UserTenant{ID: actor, UserID: actor, TenantID: "s-owner", Role: role, Status: &active})
	}
	create(t, &entity.UserTenant{ID: "s-shared-membership", UserID: "s-owner", TenantID: "s-foreign", Role: "normal", Status: &active})
	chatID, embedID, rerankID, otherEmbedID := int64(9007199254740993), int64(9007199254740995), int64(9007199254740997), int64(9007199254740999)
	legacy := func(id int64, tenant, factory, name, kind, key string) {
		create(t, &entity.TenantLLM{ID: id, TenantID: tenant, LLMFactory: factory, LLMName: &name, ModelType: &kind, APIKey: &key, APIBase: &modelBase, Status: active, MaxTokens: 8192})
	}
	legacy(chatID, "s-owner", "OpenAI", "s-chat", "chat", "s-chat-key")
	legacy(embedID, "s-owner", "OpenAI", "s-embedding", "embedding", "s-embed-key")
	legacy(rerankID, "s-owner", "Jina", "s-reranker", "rerank", "s-rerank-key")
	legacy(otherEmbedID, "s-foreign", "OpenAI", "s-embedding", "embedding", "other-embed-key")
	if err := db.Model(&entity.TenantLLM{}).Where("id = ?", otherEmbedID).Update("api_base", modelBase+"/different-embedding-space").Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Model(&entity.Tenant{}).Where("id = ?", "s-owner").Updates(map[string]any{"tenant_llm_id": chatID, "tenant_embd_id": embedID}).Error; err != nil {
		t.Fatal(err)
	}
	// A modern same-name binding must not replace the exact legacy numeric ID.
	create(t, &entity.TenantModelProvider{ID: "s-modern-provider", TenantID: "s-owner", ProviderName: "OpenAI"})
	create(t, &entity.TenantModelInstance{ID: "s-modern-instance", ProviderID: "s-modern-provider", InstanceName: "default", APIKey: "wrong-modern-key", Status: "active", Extra: `{"base_url":"http://127.0.0.1:1"}`})
	create(t, &entity.TenantModel{ID: "s-modern-model", ProviderID: "s-modern-provider", InstanceID: "s-modern-instance", ModelName: "s-chat", ModelType: "chat", Status: "active"})
	for _, kb := range []struct {
		id, tenant, permission string
		embed                  int64
	}{{"s-kb", "s-owner", "team", embedID}, {"s-private", "s-owner", "me", embedID}, {"s-other-kb", "s-foreign", "team", otherEmbedID}} {
		create(t, &entity.Knowledgebase{ID: kb.id, TenantID: kb.tenant, CreatedBy: kb.tenant, Name: kb.id, Permission: kb.permission, Status: &active, TenantEmbdID: &kb.embed, ParserConfig: entity.JSONMap{}})
	}
	name := "Report, Q1.pdf"
	create(t, &entity.Document{ID: "s-doc", KbID: "s-kb", Name: &name, Status: &active, ParserConfig: entity.JSONMap{}})
	saved := entity.JSONMap{"chat_id": json.Number(fmt.Sprint(chatID)), "tenant_rerank_id": json.Number(fmt.Sprint(rerankID)), "similarity_threshold": 0, "vector_similarity_weight": 0.3, "top_k": 0, "keyword": false, "cross_languages": []string{}, "llm_setting": map[string]interface{}{"temperature": 0, "top_p": 0, "thinking": true}}
	for _, row := range []struct {
		id, owner, status string
		config            entity.JSONMap
	}{{"s-search", "s-owner", active, saved}, {"s-deleted", "s-owner", inactive, saved}, {"s-foreign-search", "s-foreign", active, saved}, {"s-invalid-model", "s-owner", active, entity.JSONMap{"chat_id": json.Number(fmt.Sprint(embedID))}}, {"s-empty-search", "s-owner", active, entity.JSONMap{"chat_id": json.Number(fmt.Sprint(chatID)), "kb_ids": []string{}}}} {
		create(t, &entity.Search{ID: row.id, TenantID: row.owner, CreatedBy: row.owner, Name: row.id, Status: &row.status, SearchConfig: row.config})
	}
	dialog := &entity.Chat{ID: "s-dialog", TenantID: "s-owner", Status: &active, TenantLLMID: &chatID, TenantRerankID: &rerankID, PromptConfig: entity.JSONMap{"system": "Rules {knowledge}", "empty_response": "No matching knowledge.", "quote": true}, LLMSetting: entity.JSONMap{"temperature": 0, "top_p": 0, "thinking": false}, KBIDs: entity.JSONSlice{"s-kb"}, TopN: 6, TopK: 32, DoRefer: "1"}
	create(t, dialog)
	create(t, &entity.Chat{ID: "s-foreign-dialog", TenantID: "s-foreign", Status: &active, LLMSetting: entity.JSONMap{}, PromptConfig: entity.JSONMap{}, KBIDs: entity.JSONSlice{}})
	session := func(t *testing.T, id, dialog, actor string) {
		t.Helper()
		j.Event("reserve-conversation-row", map[string]any{"database": database, "id": id, "dialog_id": dialog, "actor": actor})
		create(t, &entity.ChatSession{ID: id, DialogID: dialog, UserID: &actor, Message: json.RawMessage(`{"messages":[{"role":"assistant","content":"prologue"}]}`), Reference: json.RawMessage(`[]`)})
	}
	readSession := func(t *testing.T, id string) string {
		t.Helper()
		var value string
		if err := reader.QueryRow(`SELECT jsonb_build_object('message',message,'reference',reference)::text FROM t_ai_conversations WHERE id=$1`, id).Scan(&value); err != nil {
			t.Fatal("independent session readback", err)
		}
		return value
	}

	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, nil, nil, nil, nil, handler.NewChatSessionHandler(chat, service.NewUserService()), nil, nil, nil, nil, nil, nil)
	routes.SetSearchbotHandler(handler.NewSearchbotHandlerWithAsk(service.NewSearchbotService(provider), chunks, ask))
	routes.Setup(app)
	base := after10bbeServer(t, j, "actual-Router.Setup-JWT", app)
	client := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{Proxy: nil}}
	t.Cleanup(client.CloseIdleConnections)
	do := func(t *testing.T, path string, body interface{}, actor string) (int, string) {
		t.Helper()
		raw, _ := json.Marshal(body)
		req, _ := http.NewRequest("POST", base+path, bytes.NewReader(raw))
		req.Header.Set("Content-Type", "application/json")
		if actor != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[actor])
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal("owned HTTP request", err)
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, string(data)
	}
	askBody := func(search string, kbs []string) map[string]interface{} {
		return map[string]interface{}{"question": "manual question", "kb_ids": kbs, "search_id": search}
	}
	completionBody := func(id string, stream bool) map[string]interface{} {
		return map[string]interface{}{"conversation_id": id, "stream": stream, "temperature": 0, "top_p": 0, "thinking": false, "messages": []map[string]interface{}{{"role": "user", "content": "previous question"}, {"role": "assistant", "content": "previous answer"}, {"role": "user", "content": "manual question", "id": "message"}}}
	}
	finalAnswer := func(t *testing.T, raw string) map[string]interface{} {
		t.Helper()
		var last map[string]interface{}
		if strings.HasPrefix(strings.TrimSpace(raw), "{") {
			var envelope map[string]interface{}
			if json.Unmarshal([]byte(raw), &envelope) != nil {
				t.Fatal(raw)
			}
			last, _ = envelope["data"].(map[string]interface{})
			return last
		}
		for _, line := range strings.Split(raw, "\n") {
			if !strings.HasPrefix(line, "data: ") {
				continue
			}
			var event struct{ Data json.RawMessage }
			if json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &event) != nil {
				t.Fatal(line)
			}
			var frame map[string]interface{}
			if json.Unmarshal(event.Data, &frame) == nil && frame["final"] == true {
				last = frame
			}
		}
		return last
	}
	assertAnswer := func(t *testing.T, raw string) {
		t.Helper()
		frame := finalAnswer(t, raw)
		if frame == nil {
			t.Fatal("missing final answer", raw)
		}
		text, _ := frame["answer"].(string)
		refs, _ := frame["reference"].(map[string]interface{})
		if text != "Grounded fact from manual. [ID:s-chunk]" || len(refs["chunks"].([]interface{})) != 1 || strings.Contains(text, "private") {
			t.Fatal("visible answer/reference mismatch", raw)
		}
		ref := refs["chunks"].([]interface{})[0].(map[string]interface{})
		if ref["id"] != "s-chunk" || ref["document_name"] != "Report, Q1.pdf" || len(ref["positions"].([]interface{})[0].([]interface{})) != 5 || ref["vector"] != nil {
			t.Fatal("citation projection lost", ref)
		}
	}
	counts := func() []int64 {
		return []int64{factory.calls.Load(), embedCalls.Load(), rerankCalls.Load(), chatCalls.Load(), searchCalls.Load(), hydrateCalls.Load(), metaCalls.Load()}
	}
	zeroCalls := func(t *testing.T, before []int64) {
		t.Helper()
		after := counts()
		for i := range before {
			if before[i] != after[i] {
				t.Fatalf("denied request made model/retrieval calls: before=%v after=%v", before, after)
			}
		}
	}

	t.Run("JWT-and-resource-denials", func(t *testing.T) {
		session(t, "s-auth-session", "s-dialog", "s-owner")
		beforeSQL := readSession(t, "s-auth-session")
		for _, tc := range []struct {
			actor, search string
			kbs           []string
		}{{"", "s-search", []string{"s-kb"}}, {"s-inactive", "s-search", []string{"s-kb"}}, {"s-invite", "s-search", []string{"s-kb"}}, {"s-member", "missing", []string{"s-kb"}}, {"s-member", "s-deleted", []string{"s-kb"}}, {"s-member", "s-foreign-search", []string{"s-kb"}}, {"s-member", "s-search", []string{"s-private"}}} {
			before := counts()
			_, raw := do(t, "/api/v1/searchbots/ask", askBody(tc.search, tc.kbs), tc.actor)
			wantCode := 403
			if tc.actor == "" || tc.actor == "s-inactive" {
				wantCode = 401
			}
			if !strings.Contains(raw, fmt.Sprintf(`"code":%d`, wantCode)) {
				t.Fatal("denial did not reach intended auth boundary", tc.actor, tc.search, raw)
			}
			if strings.Contains(raw, `"data":true`) || strings.Contains(raw, `"final":true`) {
				t.Fatal("denied Ask succeeded", raw)
			}
			zeroCalls(t, before)
		}
		for _, actor := range []string{"", "s-inactive", "s-member", "s-other-member", "s-foreign"} {
			before := counts()
			_, raw := do(t, "/v1/conversation/completion", completionBody("s-auth-session", false), actor)
			wantCode := 403
			if actor == "" || actor == "s-inactive" {
				wantCode = 401
			}
			if !strings.Contains(raw, fmt.Sprintf(`"code":%d`, wantCode)) {
				t.Fatal("conversation denial missed intended boundary", actor, raw)
			}
			if finalAnswer(t, raw) != nil {
				t.Fatal("foreign session succeeded", raw)
			}
			zeroCalls(t, before)
		}
		if readSession(t, "s-auth-session") != beforeSQL {
			t.Fatal("denial changed conversation")
		}
		j.Event("auth-denials-independent-readback", map[string]any{"conversation": "s-auth-session", "unchanged": true, "model_and_store_calls": 0})
	})
	t.Run("Chat-KB-denial-before-stream-output", func(t *testing.T) {
		session(t, "s-kb-denied", "s-dialog", "s-member")
		beforeSQL := readSession(t, "s-kb-denied")
		if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Update("kb_ids", entity.JSONSlice{"s-private"}).Error; err != nil {
			t.Fatal(err)
		}
		defer func() {
			if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Update("kb_ids", entity.JSONSlice{"s-kb"}).Error; err != nil {
				t.Error(err)
			}
		}()
		for _, stream := range []bool{false, true} {
			before := counts()
			_, raw := do(t, "/v1/conversation/completion", completionBody("s-kb-denied", stream), "s-member")
			if !strings.Contains(raw, `"code":403`) || strings.HasPrefix(raw, "data: ") || finalAnswer(t, raw) != nil {
				t.Fatal("KB denial started model stream or returned wrong error", raw)
			}
			zeroCalls(t, before)
			if readSession(t, "s-kb-denied") != beforeSQL {
				t.Fatal("KB denial changed session")
			}
		}
		j.Event("chat-KB-denial-independent-readback", map[string]any{"conversation": "s-kb-denied", "sync_stream": true, "no_SSE_started": true, "zero_model_store_calls": true, "unchanged": true})
	})
	t.Run("Ask-actual-automatic-citations", func(t *testing.T) {
		before := counts()
		status, raw := do(t, "/api/v1/searchbots/ask", askBody("s-search", []string{"s-kb"}), "s-member")
		if status != 200 || !strings.Contains(raw, `"data":true`) {
			t.Fatal(status, raw)
		}
		assertAnswer(t, raw)
		if embedCalls.Load() < before[1]+2 || rerankCalls.Load() != before[2]+1 || hydrateCalls.Load() != before[5]+1 {
			t.Fatal("citation embedder or hydration not wired", counts())
		}
		j.Event("ask-citation-consumer-passed", map[string]any{"numeric_chat_id": chatID, "query_and_answer_embedding": true, "owner_rerank": true, "scoped_hydration": true})
	})
	t.Run("saved-empty-and-invalid-model", func(t *testing.T) {
		for _, search := range []string{"s-invalid-model", "s-empty-search"} {
			before := embedCalls.Load() + chatCalls.Load() + rerankCalls.Load() + searchCalls.Load()
			_, raw := do(t, "/api/v1/searchbots/ask", askBody(search, []string{"s-kb"}), "s-member")
			if strings.Contains(raw, `"final":true`) || embedCalls.Load()+chatCalls.Load()+rerankCalls.Load()+searchCalls.Load() != before {
				t.Fatal("invalid or zero saved selection fell back", raw)
			}
		}
	})
	t.Run("Chat-sync-images-history-and-SQL", func(t *testing.T) {
		mode.Store("image-history")
		defer mode.Store("success")
		session(t, "s-sync", "s-dialog", "s-member")
		body := completionBody("s-sync", false)
		messages := body["messages"].([]map[string]interface{})
		messages[2]["content"] = []interface{}{map[string]interface{}{"type": "text", "text": "manual question"}, map[string]interface{}{"type": "image_url", "image_url": map[string]interface{}{"url": "https://example.invalid/image.png"}}}
		status, raw := do(t, "/v1/conversation/completion", body, "s-member")
		if status != 200 {
			t.Fatal(status, raw)
		}
		assertAnswer(t, raw)
		stored := readSession(t, "s-sync")
		if !strings.Contains(stored, "[ID:s-chunk]") || !strings.Contains(stored, "image_url") || !strings.Contains(stored, "previous answer") || strings.Contains(stored, "inline private") {
			t.Fatal("SQL answer/history/reference readback", stored)
		}
		j.Event("chat-sync-independent-readback", map[string]any{"conversation": "s-sync", "answer_reference_match": true, "images_history_preserved": true})
	})
	t.Run("Chat-stream-final-and-SQL", func(t *testing.T) {
		session(t, "s-stream", "s-dialog", "s-member")
		_, raw := do(t, "/v1/conversation/completion", completionBody("s-stream", true), "s-member")
		assertAnswer(t, raw)
		if !strings.Contains(raw, `"data":true`) || !strings.Contains(readSession(t, "s-stream"), "Grounded fact from manual. [ID:s-chunk]") {
			t.Fatal("stream or SQL mismatch", raw)
		}
		j.Event("chat-stream-independent-readback", map[string]any{"conversation": "s-stream", "final_answer_reference_match": true})
	})
	t.Run("Chat-citation-settings-and-no-answer", func(t *testing.T) {
		for _, setting := range []string{"quote-false", "do-refer-zero", "configured-no-answer"} {
			prompt := entity.JSONMap{"system": "Rules {knowledge}", "empty_response": "No matching knowledge.", "quote": setting != "quote-false"}
			refer := "1"
			if setting == "do-refer-zero" {
				refer = "0"
			}
			if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Updates(map[string]interface{}{"prompt_config": prompt, "do_refer": refer}).Error; err != nil {
				t.Fatal(err)
			}
			mode.Store("success")
			if setting == "configured-no-answer" {
				mode.Store(setting)
			}
			id := "s-" + setting
			session(t, id, "s-dialog", "s-member")
			beforeEmbed, beforeHydrate := embedCalls.Load(), hydrateCalls.Load()
			_, raw := do(t, "/v1/conversation/completion", completionBody(id, true), "s-member")
			final := finalAnswer(t, raw)
			want := "Grounded fact from manual."
			if setting == "configured-no-answer" {
				want = "No matching knowledge. "
			}
			if final == nil || final["answer"] != want || strings.Contains(raw, "[ID:") || embedCalls.Load() != beforeEmbed+1 || hydrateCalls.Load() != beforeHydrate {
				t.Fatal("disabled/no-answer citations survived", setting, raw)
			}
			refs := final["reference"].(map[string]interface{})
			if len(refs["chunks"].([]interface{})) != 0 || strings.Contains(readSession(t, id), "[ID:") {
				t.Fatal("no-answer reference persisted", raw)
			}
			j.Event("citation-settings-independent-readback", map[string]any{"setting": setting, "conversation": id, "no_answer_embedding_or_hydration": true, "refs_empty": true})
		}
		mode.Store("success")
		if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Updates(map[string]interface{}{"prompt_config": dialog.PromptConfig, "do_refer": "1"}).Error; err != nil {
			t.Fatal(err)
		}
	})
	t.Run("failures-no-success-persistence", func(t *testing.T) {
		for _, failure := range []string{"provider-failure", "retrieval-failure", "rerank-failure", "citation-failure", "no-answer"} {
			mode.Store(failure)
			id := "s-fail-" + failure
			session(t, id, "s-dialog", "s-member")
			before := readSession(t, id)
			_, raw := do(t, "/v1/conversation/completion", completionBody(id, true), "s-member")
			if strings.Contains(raw, `"final":true`) || strings.Contains(raw, `"data":true`) || readSession(t, id) != before || strings.Contains(raw, "private provider failure") {
				t.Fatal("failed generation persisted or succeeded", failure, raw)
			}
			j.Event("failure-independent-readback", map[string]any{"failure": failure, "conversation": id, "unchanged": true, "no_success_terminal": true})
		}
		mode.Store("success")
	})
	t.Run("metadata-exact-values-empty-and-failure", func(t *testing.T) {
		filter := entity.JSONMap{"method": "manual", "logic": "and", "manual": []interface{}{map[string]interface{}{"key": "revision", "op": "=", "value": json.Number("9007199254740993")}, map[string]interface{}{"key": "approved", "op": "=", "value": false}, map[string]interface{}{"key": "zero", "op": "=", "value": 0}}}
		if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Update("meta_data_filter", filter).Error; err != nil {
			t.Fatal(err)
		}
		session(t, "s-filter", "s-dialog", "s-member")
		_, raw := do(t, "/v1/conversation/completion", completionBody("s-filter", false), "s-member")
		assertAnswer(t, raw)
		filter["manual"] = []interface{}{map[string]interface{}{"key": "revision", "op": "=", "value": json.Number("9007199254740993")}}
		if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Update("meta_data_filter", filter).Error; err != nil {
			t.Fatal(err)
		}
		session(t, "s-zero-selection", "s-dialog", "s-member")
		body := completionBody("s-zero-selection", false)
		body["messages"].([]map[string]interface{})[2]["doc_ids"] = []string{}
		beforeCalls := embedCalls.Load() + chatCalls.Load() + rerankCalls.Load() + searchCalls.Load()
		_, raw = do(t, "/v1/conversation/completion", body, "s-member")
		if !strings.Contains(raw, "No matching knowledge.") || embedCalls.Load()+chatCalls.Load()+rerankCalls.Load()+searchCalls.Load() != beforeCalls {
			t.Fatal("explicit [] broadened retrieval", raw)
		}
		filter["manual"] = []interface{}{map[string]interface{}{"key": "revision", "op": "=", "value": json.Number("9007199254740992")}}
		if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Update("meta_data_filter", filter).Error; err != nil {
			t.Fatal(err)
		}
		session(t, "s-no-match", "s-dialog", "s-member")
		beforeCalls = embedCalls.Load() + chatCalls.Load() + rerankCalls.Load() + searchCalls.Load()
		_, raw = do(t, "/v1/conversation/completion", completionBody("s-no-match", false), "s-member")
		if !strings.Contains(raw, "No matching knowledge.") || embedCalls.Load()+chatCalls.Load()+rerankCalls.Load()+searchCalls.Load() != beforeCalls {
			t.Fatal("numeric metadata no-match broadened", raw)
		}
		mode.Store("filter-failure")
		session(t, "s-filter-failure", "s-dialog", "s-member")
		before := readSession(t, "s-filter-failure")
		_, raw = do(t, "/v1/conversation/completion", completionBody("s-filter-failure", true), "s-member")
		if strings.Contains(raw, `"data":true`) || readSession(t, "s-filter-failure") != before {
			t.Fatal("filter failure broadened/persisted", raw)
		}
		mode.Store("success")
		if err := db.Model(&entity.Chat{}).Where("id = ?", "s-dialog").Update("meta_data_filter", nil).Error; err != nil {
			t.Fatal(err)
		}
		if pits.Load() != 0 {
			t.Fatal("metadata PIT leaked")
		}
		j.Event("metadata-consumer-passed", map[string]any{"exact_revision": true, "false_zero_preserved": true, "explicit_empty_zero_calls": true, "no_match_no_unfiltered_fallback": true, "filter_failure_no_write": true, "PITs_remaining": pits.Load()})
	})
	t.Run("no-KB-solo-history-and-zero-retrieval", func(t *testing.T) {
		mode.Store("solo")
		defer mode.Store("success")
		j.Event("reserve-solo-dialog", map[string]any{"database": database, "dialog_id": "s-solo-dialog"})
		create(t, &entity.Chat{ID: "s-solo-dialog", TenantID: "s-owner", Status: &active, TenantLLMID: &chatID, PromptConfig: entity.JSONMap{"system": "Solo rules"}, LLMSetting: entity.JSONMap{"temperature": 0, "top_p": 0, "thinking": false}, KBIDs: entity.JSONSlice{}})
		before := embedCalls.Load() + rerankCalls.Load() + searchCalls.Load() + hydrateCalls.Load()
		for _, stream := range []bool{false, true} {
			id := fmt.Sprintf("s-solo-%v", stream)
			session(t, id, "s-solo-dialog", "s-member")
			body := completionBody(id, stream)
			body["messages"] = []map[string]interface{}{{"role": "user", "content": "solo question", "id": "solo-message"}}
			_, raw := do(t, "/v1/conversation/completion", body, "s-member")
			final := finalAnswer(t, raw)
			if final == nil || final["answer"] != "Grounded fact from manual." || strings.Contains(raw, "[ID:") || !strings.Contains(readSession(t, id), "prologue") {
				t.Fatal("solo behavior regressed", raw)
			}
		}
		if embedCalls.Load()+rerankCalls.Load()+searchCalls.Load()+hydrateCalls.Load() != before {
			t.Fatal("no-KB consumer invoked retrieval")
		}
		j.Event("solo-consumer-independent-readback", map[string]any{"retrieval_calls": 0, "sync_and_stream": true, "stored_history_preserved": true})
	})
	t.Run("cross-KB-resolved-identity", func(t *testing.T) {
		before := embedCalls.Load() + chatCalls.Load() + rerankCalls.Load() + searchCalls.Load()
		_, raw := do(t, "/api/v1/searchbots/ask", askBody("s-search", []string{"s-kb", "s-other-kb"}), "s-owner")
		if strings.Contains(raw, `"final":true`) || embedCalls.Load()+chatCalls.Load()+rerankCalls.Load()+searchCalls.Load() != before {
			t.Fatal("same-name different embedding identity accepted", raw)
		}
	})
	t.Run("sender-failure-and-terminal-commit-boundary", func(t *testing.T) {
		messages := []map[string]interface{}{{"role": "user", "content": "manual question", "id": "msg"}}
		for _, terminal := range []bool{false, true} {
			id := "s-sender-before"
			if terminal {
				id = "s-sender-terminal"
			}
			session(t, id, "s-dialog", "s-member")
			before := readSession(t, id)
			failure := errors.New("controlled sender failure")
			err := chat.CompletionStream(context.Background(), "s-member", id, messages, "", nil, "msg", func(frame string) error {
				if terminal {
					if strings.Contains(frame, `"final":true`) {
						return failure
					}
				} else if strings.Contains(frame, "Grounded fact") {
					return failure
				}
				return nil
			})
			if !errors.Is(err, failure) {
				t.Fatal("sender error lost", err)
			}
			changed := readSession(t, id) != before
			if changed != terminal {
				t.Fatal("unexpected SQL/network commit boundary", terminal, changed)
			}
			j.Event("sender-failure-independent-readback", map[string]any{"conversation": id, "terminal_after_commit": terminal, "SQL_changed": changed, "returned_sender_error": true})
		}
	})
	t.Run("HTTP-client-cancel-drains-provider-no-write", func(t *testing.T) {
		mode.Store("cancel")
		session(t, "s-cancel", "s-dialog", "s-member")
		before := readSession(t, "s-cancel")
		body, _ := json.Marshal(completionBody("s-cancel", true))
		ctx, cancel := context.WithCancel(context.Background())
		req, _ := http.NewRequestWithContext(ctx, "POST", base+"/v1/conversation/completion", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+tokens["s-member"])
		resp, err := client.Do(req)
		if err != nil {
			cancel()
			t.Fatal(err)
		}
		scan := bufio.NewScanner(resp.Body)
		found := false
		for scan.Scan() {
			if strings.Contains(scan.Text(), "Partial visible") {
				found = true
				break
			}
		}
		cancel()
		_ = resp.Body.Close()
		if !found {
			t.Fatal("cancel point missing")
		}
		select {
		case <-cancelled:
		case <-time.After(3 * time.Second):
			t.Fatal("provider did not observe request cancellation")
		}
		// Independent readback after the provider has observed cancellation.
		if readSession(t, "s-cancel") != before {
			t.Fatal("cancel persisted a successful turn")
		}
		mode.Store("success")
		j.Event("cancel-independent-readback", map[string]any{"conversation": "s-cancel", "provider_context_cancelled": true, "unchanged": true})
	})
	if pits.Load() != 0 {
		t.Fatal("PIT survived acceptance")
	}
	j.Event("controlled-consumers-complete", map[string]any{"calls": counts(), "PITs_remaining": pits.Load(), "real_provider_account": false, "Python_production_changed": false})
}
