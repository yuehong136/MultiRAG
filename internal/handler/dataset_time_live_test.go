package handler_test

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
)

// Real handlers and independent SQL readback; identity is supplied at the middleware boundary.
func TestDatasetTimeHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_DATASET_TIME_POSTGRES_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("MULTIRAG_DATASET_TIME_POSTGRES_DSN must name an owned scratch database")
		}
		t.Skip("requires an owned dataset-time scratch PostgreSQL database")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_afterd08_a_time_") {
		t.Fatal("refusing a database outside the dataset-time scratch namespace")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("connect owned scratch database failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := pool.Close(); err != nil {
			t.Error(err)
		}
	})
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("open independent SQL readback failed")
	}
	t.Cleanup(func() {
		if err := readback.Close(); err != nil {
			t.Error(err)
		}
	})
	var database string
	if err := readback.QueryRow("SELECT current_database()").Scan(&database); err != nil || database != strings.TrimPrefix(parsed.Path, "/") {
		t.Fatal("unexpected scratch database")
	}
	previousDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previousDB })
	if err := db.AutoMigrate(&entity.Knowledgebase{}, &entity.Tenant{}, &entity.User{}, &entity.UserTenant{}); err != nil {
		t.Fatal(err)
	}
	owner := strings.ReplaceAll(uuid.NewString(), "-", "")
	active := "1"
	if err := db.Create(&entity.Tenant{ID: owner, EmbdID: "test@Builtin", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	historical := make(map[string]string)
	ids := make([]string, 0, 3)
	for i, stamp := range []int64{0, 1700000000, 1700000000000} {
		id, err := uuid.NewUUID()
		if err != nil {
			t.Fatal(err)
		}
		kbID := strings.ReplaceAll(id.String(), "-", "")
		date := time.Unix(1700000000+int64(i), 0).UTC()
		kb := &entity.Knowledgebase{ID: kbID, TenantID: owner, CreatedBy: owner, Name: fmt.Sprintf("historical-%d", i), Permission: "me", EmbdID: "test@Builtin", Status: &active, ParserConfig: entity.JSONMap{}}
		kb.BaseModel = entity.BaseModel{CreateTime: &stamp, UpdateTime: &stamp, CreateDate: &date, UpdateDate: &date}
		if err := db.Create(kb).Error; err != nil {
			t.Fatal(err)
		}
		ids = append(ids, kbID)
	}
	snapshot := func(id string) string {
		t.Helper()
		var raw string
		if err := readback.QueryRow("SELECT to_jsonb(k)::text FROM t_ai_knowledgebases k WHERE id=$1", id).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	for _, id := range ids {
		historical[id] = snapshot(id)
	}

	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(func(c *gin.Context) {
		switch c.GetHeader("Authorization") {
		case "Bearer owner":
			c.Set("user", &entity.User{ID: owner})
		case "Bearer foreign":
			c.Set("user", &entity.User{ID: "foreign"})
		}
		c.Next()
	})
	datasets := handler.NewDatasetsHandler(service.NewDatasetsService())
	kbService := service.NewKnowledgebaseService()
	kbs := handler.NewKnowledgebaseHandler(kbService, nil, nil)
	app.POST("/api/v1/datasets", datasets.CreateDataset)
	app.GET("/api/v1/datasets", datasets.ListDatasets)
	app.POST("/api/v1/kb/update", kbs.UpdateKB)
	address := os.Getenv("MULTIRAG_DATASET_TIME_HTTP_ADDR")
	if address == "" {
		address = "127.0.0.1:0"
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: app, ReadHeaderTimeout: 5 * time.Second}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	t.Cleanup(func() {
		if err := server.Close(); err != nil {
			t.Error(err)
		}
		if err := <-done; err != http.ErrServerClosed {
			t.Error(err)
		}
	})
	client := &http.Client{Timeout: 10 * time.Second}
	call := func(method, path, auth string, body map[string]any) map[string]any {
		t.Helper()
		payload, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		req, err := http.NewRequest(method, "http://"+listener.Addr().String()+path, strings.NewReader(string(payload)))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if auth != "" {
			req.Header.Set("Authorization", "Bearer "+auth)
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		var out map[string]any
		if err := json.NewDecoder(response.Body).Decode(&out); err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != http.StatusOK {
			t.Fatalf("HTTP %d: %v", response.StatusCode, out)
		}
		return out
	}
	countRows := func() int {
		t.Helper()
		var count int
		if err := readback.QueryRow("SELECT count(*) FROM t_ai_knowledgebases WHERE tenant_id=$1", owner).Scan(&count); err != nil {
			t.Fatal(err)
		}
		return count
	}
	for _, auth := range []string{"", "foreign"} {
		before := countRows()
		out := call("POST", "/api/v1/datasets", auth, map[string]any{"name": "denied"})
		if out["code"] == float64(common.CodeSuccess) || countRows() != before {
			t.Fatal("denied creation changed SQL", out)
		}
	}
	beforeCreate := time.Now().Truncate(time.Second)
	created := call("POST", "/api/v1/datasets", "owner", map[string]any{"name": "dataset-time"})
	if created["code"] != float64(common.CodeSuccess) {
		t.Fatal(created)
	}
	data := created["data"].(map[string]any)
	kbID := data["id"].(string)
	type storedTime struct {
		createTime, updateTime, documents, chunks, tokens int64
		createDate, updateDate                            time.Time
		name                                              string
	}
	read := func() storedTime {
		t.Helper()
		var row storedTime
		err := readback.QueryRow("SELECT create_time, update_time, create_date, update_date, doc_num, chunk_num, token_num, name FROM t_ai_knowledgebases WHERE id=$1", kbID).Scan(&row.createTime, &row.updateTime, &row.createDate, &row.updateDate, &row.documents, &row.chunks, &row.tokens, &row.name)
		if err != nil {
			t.Fatal(err)
		}
		return row
	}
	checkTime := func(stamp int64, date time.Time, before time.Time) {
		t.Helper()
		if stamp < before.UnixMilli() || stamp > time.Now().UnixMilli() || date.Nanosecond() != 0 || date.Unix() != stamp/1000 {
			t.Fatalf("millisecond epoch and second-resolution date disagree: %d %s", stamp, date)
		}
	}
	row := read()
	checkTime(row.createTime, row.createDate, beforeCreate)
	checkTime(row.updateTime, row.updateDate, beforeCreate)
	if row.createTime != row.updateTime || !row.createDate.Equal(row.updateDate) || data["create_time"] != float64(row.createTime) || data["update_time"] != float64(row.updateTime) || row.documents != 0 || row.chunks != 0 || row.tokens != 0 {
		t.Fatal("creation response/zero counts/SQL disagree", data, row)
	}
	creationTime, creationDate := row.createTime, row.createDate
	resetUpdate := func() time.Time {
		t.Helper()
		if _, err := readback.Exec("UPDATE t_ai_knowledgebases SET update_time=0, update_date=$1 WHERE id=$2", time.Unix(0, 0).UTC(), kbID); err != nil {
			t.Fatal(err)
		}
		return time.Now().Truncate(time.Second)
	}
	updateBody := map[string]any{"kb_id": kbID, "name": "renamed", "parser_id": "naive", "pagerank": 0}
	beforeUpdate := resetUpdate()
	updated := call("POST", "/api/v1/kb/update", "owner", updateBody)
	if updated["code"] != float64(common.CodeSuccess) {
		t.Fatal(updated)
	}
	row = read()
	checkTime(row.updateTime, row.updateDate, beforeUpdate)
	if row.name != "renamed" || updated["data"].(map[string]any)["update_time"] != float64(row.updateTime) {
		t.Fatal("update response/SQL disagree", updated, row)
	}
	for _, auth := range []string{"", "foreign"} {
		before := snapshot(kbID)
		denied := call("POST", "/api/v1/kb/update", auth, updateBody)
		if denied["code"] == float64(common.CodeSuccess) || snapshot(kbID) != before {
			t.Fatal("denied update changed SQL", denied)
		}
	}
	beforeIncrease := resetUpdate()
	if err := kbService.AtomicIncreaseDocNumByID(kbID); err != nil {
		t.Fatal(err)
	}
	row = read()
	checkTime(row.updateTime, row.updateDate, beforeIncrease)
	if row.documents != 1 || row.chunks != 0 || row.tokens != 0 {
		t.Fatal("increment counters", row)
	}
	const workers = 8
	var wait sync.WaitGroup
	failures := make(chan error, workers)
	for i := 0; i < workers; i++ {
		wait.Add(1)
		go func() { defer wait.Done(); failures <- kbService.AtomicIncreaseDocNumByID(kbID) }()
	}
	wait.Wait()
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	row = read()
	if row.documents != 1+workers {
		t.Fatal("atomic increments lost", row)
	}
	if _, err := readback.Exec("UPDATE t_ai_knowledgebases SET chunk_num=5, token_num=13 WHERE id=$1", kbID); err != nil {
		t.Fatal(err)
	}
	beforeDecrease := resetUpdate()
	if err := kbService.DecreaseDocumentNum(kbID, 1+workers, 5, 13); err != nil {
		t.Fatal(err)
	}
	row = read()
	checkTime(row.updateTime, row.updateDate, beforeDecrease)
	if row.documents != 0 || row.chunks != 0 || row.tokens != 0 {
		t.Fatal("decrement to legal zero", row)
	}
	beforeZero := resetUpdate()
	if err := kbService.DecreaseDocumentNum(kbID, 0, 0, 0); err != nil {
		t.Fatal(err)
	}
	row = read()
	checkTime(row.updateTime, row.updateDate, beforeZero)
	if row.documents != 0 || row.chunks != 0 || row.tokens != 0 || row.createTime != creationTime || !row.createDate.Equal(creationDate) {
		t.Fatal("zero decrement or creation fields changed", row)
	}

	for _, orderby := range []string{"create_time", "update_time"} {
		for _, desc := range []bool{false, true} {
			out := call("GET", fmt.Sprintf("/api/v1/datasets?orderby=%s&desc=%t&page_size=10", orderby, desc), "owner", nil)
			if out["code"] != float64(common.CodeSuccess) || out["total_datasets"] != float64(4) {
				t.Fatal(out)
			}
			rows := out["data"].([]any)
			got := make([]string, 0, len(rows))
			for _, value := range rows {
				item := value.(map[string]any)
				got = append(got, item["id"].(string))
				if item["id"] == ids[0] && (item["update_time"] != float64(0) || item["document_count"] != float64(0)) {
					t.Fatal("legal zero lost from list", item)
				}
			}
			want := append(append([]string{}, ids...), kbID)
			if desc {
				for i, j := 0, len(want)-1; i < j; i, j = i+1, j-1 {
					want[i], want[j] = want[j], want[i]
				}
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("%s desc=%t got %v want %v", orderby, desc, got, want)
			}
		}
	}

	consumer := cli.NewMultiRAGClient("user")
	consumer.HTTPClient.Host = "127.0.0.1"
	consumer.HTTPClient.Port = listener.Addr().(*net.TCPAddr).Port
	consumer.HTTPClient.APIKey = "owner"
	response, err := consumer.ExecuteCommand(cli.NewCommand("list_user_datasets"))
	if err != nil {
		t.Fatal("real CLI list failed", err)
	}
	cliRows := response.(*cli.CommonResponse).Data
	if len(cliRows) != 4 {
		t.Fatal("CLI lost dataset rows", cliRows)
	}
	var found bool
	for _, item := range cliRows {
		if item["id"] == kbID {
			found = true
			if item["update_time"] != float64(row.updateTime) || item["document_count"] != float64(0) {
				t.Fatal("CLI lost millisecond time or legal zero", item)
			}
		}
	}
	if !found {
		t.Fatal("CLI lost new dataset")
	}
	beforeFailure := snapshot(kbID)
	if err := db.Exec(`CREATE FUNCTION dataset_time_reject_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled update failure'; END $$`).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`CREATE TRIGGER dataset_time_reject_update BEFORE UPDATE ON t_ai_knowledgebases FOR EACH ROW EXECUTE FUNCTION dataset_time_reject_update()`).Error; err != nil {
		t.Fatal(err)
	}

	if err := db.Exec(`CREATE TRIGGER dataset_time_reject_insert BEFORE INSERT ON t_ai_knowledgebases FOR EACH ROW EXECUTE FUNCTION dataset_time_reject_update()`).Error; err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, query := range []string{"DROP TRIGGER dataset_time_reject_insert ON t_ai_knowledgebases", "DROP TRIGGER dataset_time_reject_update ON t_ai_knowledgebases", "DROP FUNCTION dataset_time_reject_update()"} {
			if err := db.Exec(query).Error; err != nil {
				t.Error(err)
			}
		}
	})
	failedCreate := call("POST", "/api/v1/datasets", "owner", map[string]any{"name": "failed-insert"})
	if failedCreate["code"] != float64(common.CodeServerError) || countRows() != 4 {
		t.Fatal("failed create changed SQL", failedCreate)
	}
	failed := call("POST", "/api/v1/kb/update", "owner", updateBody)
	if failed["code"] != float64(common.CodeServerError) || snapshot(kbID) != beforeFailure {
		t.Fatal("failed update changed SQL", failed)
	}
	for _, operation := range []func() error{func() error { return kbService.AtomicIncreaseDocNumByID(kbID) }, func() error { return kbService.DecreaseDocumentNum(kbID, 1, 1, 1) }} {
		if err := operation(); err == nil || snapshot(kbID) != beforeFailure {
			t.Fatal("failed counter update changed SQL", err)
		}
	}
	for id, before := range historical {
		if snapshot(id) != before {
			t.Fatal("historical record was modified", id)
		}
	}
	if countRows() != 4 {
		t.Fatal("unexpected row mutation")
	}
	t.Log("real HTTP create/update/list + independent SQL: millisecond/date parity; atomic increments; decrement/zero; sorting; historical/denial/failure preservation")
}
