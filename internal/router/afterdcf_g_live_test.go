package router

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image/png"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine/elasticsearch"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

func TestAfterDCFRouterScratch(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Memory{}, &entity.File{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.File2Document{}); err != nil {
		t.Fatal(err)
	}
	p := f.Prefix
	owner, member, foreign := p+"owner", p+"member", p+"foreign"
	active := "1"
	credentialsByActor := map[string]string{}
	previousSecret := server.GetSecretKey()
	keyPath := filepath.Join(os.Getenv("TMPDIR"), p+"-signing.yaml")
	f.Journal.Event("reserve-signing-config", map[string]any{"path": keyPath})
	keyFile, err := os.OpenFile(keyPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal("create owned signing config")
	}
	if _, err = keyFile.WriteString("multirag:\n  secret_key: " + p + "controlled-signing-secret\n"); err != nil {
		keyFile.Close()
		t.Fatal("write signing config")
	}
	if err = keyFile.Sync(); err != nil {
		keyFile.Close()
		t.Fatal(err)
	}
	keyFile.Close()
	t.Cleanup(func() {
		if err := os.Remove(keyPath); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(keyPath)
		f.Journal.Event("cleanup-signing-config", map[string]any{"path": keyPath, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("signing config survived cleanup")
		}
	})
	if err = server.FromConfigFile(keyPath); err != nil {
		t.Fatal("read owned signing config")
	}
	if err = server.InitVariables(nil); err != nil {
		t.Fatal("initialize signing config")
	}
	t.Cleanup(func() { server.SetSecretKey(previousSecret) })
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned Go API acceptance")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })
	for _, id := range []string{owner, member, foreign} {
		token := p + "session"
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@example.test", Nickname: id, AccessToken: &token, Status: &active, AccountKind: "local"}).Error; err != nil {
			t.Fatal(err)
		}
		credential, err := utility.SignUserJWT(id+"@example.test", server.GetSecretKey())
		if err != nil {
			t.Fatal("sign controlled JWT")
		}
		credentialsByActor[id] = credential
	}
	for _, id := range []string{owner, foreign} {
		if err := f.DB.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.UserTenant{ID: member, UserID: member, TenantID: owner, Role: "normal", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	apiToken := "multirag-" + p
	f.Journal.Event("reserve-api-credential", map[string]any{"tenant": owner, "kind": "API-token"})
	if err := f.DB.Create(&entity.APIToken{TenantID: owner, Token: apiToken}).Error; err != nil {
		t.Fatal(err)
	}
	credentialsByActor["api"] = apiToken
	if f.Config.Elasticsearch.Hosts == "" {
		t.Fatal("real loopback Elasticsearch config required")
	}
	esURL, err := url.Parse(f.Config.Elasticsearch.Hosts)
	if err != nil || (esURL.Hostname() != "127.0.0.1" && esURL.Hostname() != "localhost") {
		t.Fatal("loopback ES endpoint required")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	t.Cleanup(transport.CloseIdleConnections)
	client := &http.Client{Transport: transport, Timeout: 10 * time.Second}
	doES := func(ctx context.Context, method, path string, body any) (int, []byte) {
		t.Helper()
		var data []byte
		if body != nil {
			data, err = json.Marshal(body)
			if err != nil {
				t.Fatal(err)
			}
		}
		req, err := http.NewRequestWithContext(ctx, method, strings.TrimSuffix(f.Config.Elasticsearch.Hosts, "/")+path, bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if f.Config.Elasticsearch.Username != "" {
			req.SetBasicAuth(f.Config.Elasticsearch.Username, f.Config.Elasticsearch.Password)
		}
		res, err := client.Do(req)
		if err != nil {
			t.Fatal("real ES request failed")
		}
		defer res.Body.Close()
		raw, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatal("ES readback failed")
		}
		return res.StatusCode, raw
	}
	index := "memory_" + owner
	f.Journal.Event("reserve-memory-index", map[string]any{"endpoint": esURL.Scheme + "://" + esURL.Host, "index": index, "schema": "memory_id keyword/message_id long/forget_at keyword; no kb_id or source id"})
	if status, _ := doES(t.Context(), "HEAD", "/"+index, nil); status != 404 {
		t.Fatal("scratch ES index precondition", status)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		status, _ := doES(ctx, "DELETE", "/"+index, nil)
		if status != 200 && status != 404 {
			t.Error("delete scratch ES index")
		}
		status, _ = doES(ctx, "HEAD", "/"+index, nil)
		f.Journal.Event("cleanup-memory-index", map[string]any{"endpoint": esURL.Scheme + "://" + esURL.Host, "index": index, "absent": status == 404})
		if status != 404 {
			t.Error("independent ES cleanup readback")
		}
	})
	mapping := map[string]any{"mappings": map[string]any{"dynamic": "strict", "properties": map[string]any{"memory_id": map[string]any{"type": "keyword"}, "message_id": map[string]any{"type": "long"}, "forget_at": map[string]any{"type": "keyword"}}}}
	if status, _ := doES(t.Context(), "PUT", "/"+index, mapping); status != 200 {
		t.Fatal("create exact message schema", status)
	}
	memoryID, neighbor := p+"memory", p+"neighbor"
	for _, id := range []string{memoryID, neighbor} {
		if err := f.DB.Create(&entity.Memory{ID: id, TenantID: owner, Name: id, EmbdID: "", LLMID: "", Permissions: "team"}).Error; err != nil {
			t.Fatal(err)
		}
		if status, _ := doES(t.Context(), "PUT", "/"+index+"/_doc/"+id+"_0?refresh=true", map[string]any{"memory_id": id, "message_id": 0}); status != 200 && status != 201 {
			t.Fatal("seed exact memory message", status)
		}
	}
	engine, err := elasticsearch.NewEngine(&f.Config.Elasticsearch)
	if err != nil {
		t.Fatal("create actual ES message adapter")
	}
	t.Cleanup(func() { engine.Close() })
	previousStore := storage.GetStorageFactory().GetStorage()
	storage.GetStorageFactory().SetStorage(f.Store)
	t.Cleanup(func() { storage.GetStorageFactory().SetStorage(previousStore) })
	id := strings.Repeat("a", 32)
	raw, _ := json.Marshal(service.AgentUploadDescriptor{ID: id, CreatedBy: owner, Name: "active.html", Size: 3, Extension: "html", MIMEType: "text/html", CreatedAt: 1})
	for key, data := range map[string][]byte{id: []byte("abc"), id + ".upload.json": raw} {
		if err := f.Store.Put(owner+"-downloads", key, data); err != nil {
			t.Fatal(err)
		}
	}
	smtp := testsupport.NewSMTP(t)
	smtpHost, smtpPort := smtp.HostPort()
	users := service.NewUserService()
	users.SetPasswordRecoveryService(service.NewPasswordRecoveryService(f.DB, f.Redis, common.SMTPConfig{MailServer: smtpHost, MailPort: smtpPort, MailFromAddress: "no-reply@example.test", TimeoutSeconds: 2}))
	gin.SetMode(gin.TestMode)
	app := gin.New()
	r := NewRouter(handler.NewAuthHandler(), handler.NewUserHandler(users), nil, handler.NewDocumentHandler(service.NewDocumentService()), nil, nil, nil, nil, nil, nil, nil, nil, handler.NewFileHandler(service.NewFileService(), users), handler.NewMemoryHandler(service.NewMemoryService(engine)), nil, nil)
	r.Setup(app)
	httpServer := testsupport.NewHTTP(t, app)
	config := server.GetConfig()
	priorOrigins := config.Authentication.PasswordRecoveryOrigins
	config.Authentication.PasswordRecoveryOrigins = []string{"http://localhost:5173"}
	t.Cleanup(func() { config.Authentication.PasswordRecoveryOrigins = priorOrigins })
	preflight, _ := http.NewRequest("OPTIONS", httpServer.URL+"/api/v1/auth/password/reset", nil)
	preflight.Header.Set("Origin", "http://localhost:5173")
	preflight.Header.Set("Sec-Fetch-Site", "same-site")
	preflight.Header.Set("Access-Control-Request-Method", "POST")
	preflight.Header.Set("Access-Control-Request-Headers", "content-type")
	preflightResponse, err := client.Do(preflight)
	if err != nil {
		t.Fatal("exact configured recovery preflight")
	}
	preflightResponse.Body.Close()
	if preflightResponse.StatusCode != 204 || preflightResponse.Header.Get("Access-Control-Allow-Origin") != "http://localhost:5173" || preflightResponse.Header.Get("Access-Control-Allow-Credentials") != "true" {
		t.Fatal("exact credentialed recovery CORS")
	}
	preflight.Header.Set("Sec-Fetch-Site", "cross-site")
	preflightResponse, err = client.Do(preflight)
	if err != nil {
		t.Fatal(err)
	}
	preflightResponse.Body.Close()
	if preflightResponse.StatusCode != 403 {
		t.Fatal("cross-site recovery allowed")
	}
	jarA, _ := cookiejar.New(nil)
	jarB, _ := cookiejar.New(nil)
	clientA := &http.Client{Transport: transport, Timeout: 10 * time.Second, Jar: jarA}
	clientB := &http.Client{Transport: transport, Timeout: 10 * time.Second, Jar: jarB}
	requestAs := func(httpClient *http.Client, method, path, actor string, body any, origin string) (int, http.Header, []byte) {
		t.Helper()
		var data []byte
		if body != nil {
			data, err = json.Marshal(body)
			if err != nil {
				t.Fatal(err)
			}
		}
		req, err := http.NewRequest(method, httpServer.URL+path, bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		if actor != "" {
			req.Header.Set("Authorization", "Bearer "+credentialsByActor[actor])
		}
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if origin != "" {
			req.Header.Set("Origin", origin)
		}
		res, err := httpClient.Do(req)
		if err != nil {
			t.Fatal("actual router HTTP request failed")
		}
		defer res.Body.Close()
		raw, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatal(err)
		}
		return res.StatusCode, res.Header, raw
	}
	request := func(method, path, actor string, body any) (int, http.Header, []byte) {
		return requestAs(clientA, method, path, actor, body, "")
	}
	jsonRequest := func(method, path, actor string, body any, want common.ErrorCode, wantData any) http.Header {
		t.Helper()
		status, headers, raw := request(method, path, actor, body)
		var result struct {
			Code common.ErrorCode `json:"code"`
			Data any              `json:"data"`
		}
		if json.Unmarshal(raw, &result) != nil || status != 200 || result.Code != want {
			t.Fatalf("business route %s returned status=%d code=%d want=%d", path, status, result.Code, want)
		}
		if wantData != nil && result.Data != wantData {
			t.Fatal("unexpected route data")
		}
		if strings.HasPrefix(path, "/api/v1/auth/password/") && headers.Get("Authorization") != "" {
			t.Fatal("recovery route issued a login")
		}
		return headers
	}
	for _, actor := range []string{owner, "api"} {
		status, headers, body := request("GET", "/api/v1/agents/download?id="+id, actor, nil)
		if status != 200 || string(body) != "abc" || headers.Get("Content-Type") != "application/octet-stream" || headers.Get("Cache-Control") != "no-store" || headers.Get("X-Content-Type-Options") != "nosniff" {
			t.Fatal("binary download contract")
		}
	}
	objectKeys := f.ObjectKeys(t)
	status, headers, body := request("GET", "/api/v1/agents/download?id="+id, foreign, nil)
	if status != 404 || len(body) != 0 || headers.Get("Cache-Control") != "no-store" || fmt.Sprint(objectKeys) != fmt.Sprint(f.ObjectKeys(t)) {
		t.Fatal("foreign download side effect")
	}
	status, headers, _ = request("GET", "/api/v1/agents/download?id="+id, "", nil)
	if status != 401 || headers.Get("Cache-Control") != "no-store" {
		t.Fatal("download auth denial cache contract")
	}
	jsonRequest("DELETE", "/api/v1/messages/"+memoryID+":0", owner, nil, common.CodeSuccess, true)
	_, firstRaw := doES(t.Context(), "GET", "/"+index+"/_doc/"+memoryID+"_0", nil)
	var first struct {
		Source struct {
			ForgetAt string `json:"forget_at"`
		} `json:"_source"`
	}
	if json.Unmarshal(firstRaw, &first) != nil || first.Source.ForgetAt == "" {
		t.Fatal("real ES forget did not persist")
	}
	jsonRequest("DELETE", "/api/v1/messages/"+memoryID+":0", member, nil, common.CodeSuccess, true)
	_, secondRaw := doES(t.Context(), "GET", "/"+index+"/_doc/"+memoryID+"_0", nil)
	var second struct {
		Source struct {
			ForgetAt string `json:"forget_at"`
		} `json:"_source"`
	}
	json.Unmarshal(secondRaw, &second)
	if second.Source.ForgetAt != first.Source.ForgetAt {
		t.Fatal("repeated forget changed timestamp")
	}
	jsonRequest("DELETE", "/api/v1/messages/"+neighbor+":0", foreign, nil, common.CodeNotFound, false)
	_, neighborRaw := doES(t.Context(), "GET", "/"+index+"/_doc/"+neighbor+"_0", nil)
	var otherDoc map[string]any
	json.Unmarshal(neighborRaw, &otherDoc)
	if _, exists := otherDoc["_source"].(map[string]any)["forget_at"]; exists {
		t.Fatal("foreign message write")
	}
	jsonRequest("DELETE", "/api/v1/messages/"+memoryID+":+1", owner, nil, common.CodeArgumentError, nil)
	// A real SQL/object consumer reads the document created through the router.
	root, file, kb := p+"root", p+"file", p+"kb"
	location := "source.txt"
	for _, row := range []any{&entity.File{ID: root, ParentID: root, TenantID: owner, CreatedBy: owner, Name: "/", Type: "folder"}, &entity.File{ID: file, ParentID: root, TenantID: owner, CreatedBy: owner, Name: location, Type: "doc", Location: &location, SourceType: "local", Size: 3}, &entity.Knowledgebase{ID: kb, TenantID: owner, CreatedBy: owner, Name: kb, EmbdID: "", ParserID: "naive", ParserConfig: entity.JSONMap{}, Permission: "team", Status: &active}} {
		if err := f.DB.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.Store.Put(root, location, []byte("xyz")); err != nil {
		t.Fatal(err)
	}
	jsonRequest("POST", "/api/v1/files/link-to-datasets?mode=add", owner, map[string]any{"file_ids": []string{file}, "kb_ids": []string{kb}}, common.CodeSuccess, true)
	var docID string
	if err := f.Read.QueryRow("SELECT id FROM t_ai_documents WHERE kb_id=$1", kb).Scan(&docID); err != nil {
		t.Fatal("independent converted SQL readback")
	}
	status, _, body = request("GET", "/api/v1/datasets/"+kb+"/documents/"+docID, "api", nil)
	if status != 200 || string(body) != "xyz" {
		t.Fatal("actual converted-document HTTP consumer")
	}
	email := owner + "@example.test"
	status, headers, body = request("POST", "/api/v1/auth/password/forgot/captcha?email="+url.QueryEscape(email), "", nil)
	if status != 200 || headers.Get("Content-Type") != "image/png" {
		t.Fatal("raw captcha HTTP contract")
	}
	if _, err := png.Decode(bytes.NewReader(body)); err != nil {
		t.Fatal("captcha Blob is not an image")
	}
	captcha, err := f.Redis.Get(t.Context(), utility.CaptchaRedisKey(email)).Result()
	if err != nil {
		t.Fatal("email-key captcha state")
	}
	jsonRequest("POST", "/api/v1/auth/password/forgot/otp", "", map[string]any{"email": email, "captcha": captcha}, common.CodeSuccess, true)
	messages := smtp.Messages()
	if len(messages) != 1 {
		t.Fatal("controlled SMTP receipt missing")
	}
	match := regexp.MustCompile(`reset code is: ([A-Z]{4})`).FindStringSubmatch(messages[0])
	if len(match) != 2 {
		t.Fatal("controlled SMTP code missing")
	}
	verifyHeaders := jsonRequest("POST", "/api/v1/auth/password/forgot/otp/verify", "", map[string]any{"email": email, "otp": match[1]}, common.CodeSuccess, true)
	cookies := (&http.Response{Header: verifyHeaders}).Cookies()
	if len(cookies) != 1 || cookies[0].Name != "multirag_recovery" || len(cookies[0].Value) != 64 || !cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteStrictMode || cookies[0].MaxAge != 300 || cookies[0].Path != "/api/v1/auth/password" || cookies[0].Domain != "" || cookies[0].Secure {
		t.Fatal("bound loopback recovery cookie contract")
	}
	oldCookie := *cookies[0]
	boundProof, err := f.Redis.Get(t.Context(), utility.OTPVerifiedRedisKey(email)).Result()
	if err != nil || boundProof == oldCookie.Value || !strings.HasPrefix(boundProof, "session:") {
		t.Fatal("bound proof digest not persisted")
	}
	beforePassword := ""
	if err := f.Read.QueryRow("SELECT COALESCE(password,'') FROM t_ai_users WHERE id=$1", owner).Scan(&beforePassword); err != nil {
		t.Fatal(err)
	}
	resetBody := map[string]any{"email": email, "new_password": "new plain password", "confirm_new_password": "new plain password"}
	assertDenied := func(httpClient *http.Client, origin string, wantStatus int, want common.ErrorCode) {
		t.Helper()
		status, _, body := requestAs(httpClient, "POST", "/api/v1/auth/password/reset", "", resetBody, origin)
		var result struct {
			Code common.ErrorCode `json:"code"`
			Data bool             `json:"data"`
		}
		if status != wantStatus || json.Unmarshal(body, &result) != nil || result.Code != want || result.Data {
			t.Fatal("unauthorized recovery client accepted", status, result.Code)
		}
		var password string
		if err := f.Read.QueryRow("SELECT COALESCE(password,'') FROM t_ai_users WHERE id=$1", owner).Scan(&password); err != nil || password != beforePassword {
			t.Fatal("unauthorized recovery changed SQL password")
		}
		if proof, err := f.Redis.Get(t.Context(), utility.OTPVerifiedRedisKey(email)).Result(); err != nil || proof != boundProof {
			t.Fatal("unauthorized recovery consumed another client's proof")
		}
	}
	assertDenied(clientB, "", 200, common.CodeAuthenticationError)
	apiURL, _ := url.Parse(httpServer.URL)
	wrongCookie := oldCookie
	wrongCookie.Value = strings.Repeat("0", 64)
	jarB.SetCookies(apiURL, []*http.Cookie{&wrongCookie})
	assertDenied(clientB, "", 200, common.CodeAuthenticationError)
	assertDenied(clientA, "https://hostile.example.test", 403, common.CodeForbidden)
	// A real SQL failure must leave this same browser credential retryable.
	if err := f.DB.Exec(`CREATE FUNCTION reject_g_http_reset() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled password failure'; END $$; CREATE TRIGGER reject_g_http_reset BEFORE UPDATE OF password ON t_ai_users FOR EACH ROW EXECUTE FUNCTION reject_g_http_reset()`).Error; err != nil {
		t.Fatal(err)
	}
	jsonRequest("POST", "/api/v1/auth/password/reset", "", resetBody, common.CodeServerError, false)
	if proof, err := f.Redis.Get(t.Context(), utility.OTPVerifiedRedisKey(email)).Result(); err != nil || proof != boundProof {
		t.Fatal("HTTP SQL failure did not restore bound proof")
	}
	if err := f.DB.Exec("DROP TRIGGER reject_g_http_reset ON t_ai_users; DROP FUNCTION reject_g_http_reset()").Error; err != nil {
		t.Fatal(err)
	}
	resetHeaders := jsonRequest("POST", "/api/v1/auth/password/reset", "", resetBody, common.CodeSuccess, true)
	clearedCookies := (&http.Response{Header: resetHeaders}).Cookies()
	if len(clearedCookies) != 1 || clearedCookies[0].Name != "multirag_recovery" || clearedCookies[0].MaxAge >= 0 || !clearedCookies[0].HttpOnly {
		t.Fatal("recovery cookie not cleared")
	}
	jarB.SetCookies(apiURL, []*http.Cookie{&oldCookie})
	status, _, body = requestAs(clientB, "POST", "/api/v1/auth/password/reset", "", resetBody, "")
	var replay struct {
		Code common.ErrorCode `json:"code"`
		Data bool             `json:"data"`
	}
	if status != 200 || json.Unmarshal(body, &replay) != nil || replay.Code != common.CodeAuthenticationError || replay.Data {
		t.Fatal("consumed recovery credential replayed")
	}

	var password, session string
	if err := f.Read.QueryRow("SELECT password,access_token FROM t_ai_users WHERE id=$1", owner).Scan(&password, &session); err != nil || !users.VerifyPassword(password, "new plain password") || session != p+"session" {
		t.Fatal("reset SQL/session readback")
	}
	if status, _ := doES(t.Context(), "DELETE", "/"+index, nil); status != 200 {
		t.Fatal("remove scratch message store")
	}
	jsonRequest("DELETE", "/api/v1/messages/"+memoryID+":0", owner, nil, common.CodeServerError, false)
}
