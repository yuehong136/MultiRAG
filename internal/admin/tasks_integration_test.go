//go:build integration

package admin

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/utility"
)

type tasksTestSecretStore struct{}

func (tasksTestSecretStore) Get(string) (string, error) {
	return strings.Repeat("task-test-secret-", 3), nil
}
func (tasksTestSecretStore) Set(string, string, time.Duration) bool   { return true }
func (tasksTestSecretStore) SetNX(string, string, time.Duration) bool { return true }

// TestAdminTasksIntegration requires an explicitly supplied disposable PostgreSQL
// database. It never reads the application's database configuration.
func TestAdminTasksIntegration(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_GO_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Fatal("MULTIRAG_GO_TEST_POSTGRES_DSN must name an isolated scratch database")
	}
	config := &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)}
	db, err := gorm.Open(postgres.Open(dsn), config)
	if err != nil {
		t.Fatal("scratch database connection failed")
	}
	connection, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	var name string
	if err := db.Raw("SELECT current_database()").Scan(&name).Error; err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(name, "multirag_test_") {
		t.Fatal("refusing non-scratch database")
	}
	// The supplied scratch database must be empty: no migration or deletion of
	// pre-existing task/user tables is allowed.
	if db.Migrator().HasTable(&entity.User{}) || db.Migrator().HasTable(&entity.Task{}) {
		t.Fatal("scratch database must have no user/task tables")
	}
	if err := db.AutoMigrate(&entity.User{}, &entity.Task{}); err != nil {
		t.Fatal(err)
	}
	previous := dao.DB
	dao.DB = db
	defer func() { dao.DB = previous }()
	if err := server.InitVariables(tasksTestSecretStore{}); err != nil {
		t.Fatal(err)
	}
	status := "1"
	yes, no := true, false
	users := []*entity.User{
		{ID: "admin", Email: "admin@example.invalid", Nickname: "admin", Status: &status, IsSuperuser: &yes, IsActive: true, IsAuthenticated: true},
		{ID: "user", Email: "user@example.invalid", Nickname: "user", Status: &status, IsSuperuser: &no, IsActive: true, IsAuthenticated: true},
		{ID: "inactive", Email: "inactive@example.invalid", Nickname: "inactive", Status: &status, IsSuperuser: &yes, IsActive: true, IsAuthenticated: true},
	}
	if err := db.Create(&users).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Model(users[2]).Update("is_active", false).Error; err != nil {
		t.Fatal(err)
	}
	chunkIDs := []string{"", "one", "one two", " one  two\tthree\n"}
	tasks := []*entity.Task{{ID: "nil", DocID: "document", Priority: 1, ProcessDuration: 2, Progress: 0.5, RetryCount: 2}}
	for i := range chunkIDs {
		tasks = append(tasks, &entity.Task{ID: strconv.Itoa(i), DocID: "document", ChunkIDs: &chunkIDs[i]})
	}
	if err := db.Create(&tasks).Error; err != nil {
		t.Fatal(err)
	}
	// An independent connection observes committed rows and denial side effects.
	observer, err := gorm.Open(postgres.Open(dsn), config)
	if err != nil {
		t.Fatal("readback connection failed")
	}
	observerSQL, err := observer.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer observerSQL.Close()
	snapshot := func() string {
		var readTasks []map[string]interface{}
		var readUsers []map[string]interface{}
		if err := observer.Table("t_ai_tasks").Order("id").Find(&readTasks).Error; err != nil {
			t.Fatal(err)
		}
		if err := observer.Table("t_ai_users").Order("id").Find(&readUsers).Error; err != nil {
			t.Fatal(err)
		}
		encoded, err := json.Marshal([]any{readTasks, readUsers})
		if err != nil {
			t.Fatal(err)
		}
		return string(encoded)
	}
	before := snapshot()
	var taskReads atomic.Int64
	if err := db.Callback().Query().Before("gorm:query").Register("tasks_test_read_counter", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_tasks" {
			taskReads.Add(1)
		}
	}); err != nil {
		t.Fatal(err)
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	NewRouter(NewHandler(NewService())).Setup(app)
	httpServer := httptest.NewServer(app)
	defer httpServer.Close()
	tokens := map[string]string{}
	for _, u := range users {
		token, err := utility.SignUserJWT(u.Email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[u.ID] = token
	}
	for _, test := range []struct {
		token        string
		status, code int
	}{
		{"", 401, 401}, {"invalid", 401, int(common.CodeUnauthorized)},
		{tokens["user"], 403, int(common.CodeForbidden)}, {tokens["inactive"], 401, int(common.CodeUnauthorized)},
	} {
		taskReads.Store(0)
		request, _ := http.NewRequest(http.MethodGet, httpServer.URL+"/api/v1/admin/tasks", nil)
		request.Header.Set("Authorization", test.token)
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		var result ErrorResponse
		err = json.NewDecoder(response.Body).Decode(&result)
		response.Body.Close()
		if err != nil || response.StatusCode != test.status || result.Code != test.code {
			t.Fatalf("denied response: %d %+v %v", response.StatusCode, result, err)
		}
		if taskReads.Load() != 0 || snapshot() != before {
			t.Fatal("denied request read tasks or altered committed state")
		}
	}
	// Exercise the real CLI parser, registration, transport, router, auth, service
	// and DAO over HTTP. Admin requests retain the raw signed token contract.
	address, _ := url.Parse(httpServer.URL)
	port, _ := strconv.Atoi(address.Port())
	client := cli.NewMultiRAGClient("admin")
	client.HTTPClient.Host = address.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.LoginToken = tokens["admin"]
	command, err := cli.NewParser("LIST TASKS;").Parse(true)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(command)
	if err != nil {
		t.Fatal(err)
	}
	rows := result.(*cli.CommonResponse).Data
	if len(rows) != len(tasks) {
		t.Fatalf("task count %d", len(rows))
	}
	want := map[string]float64{"nil": 0, "0": 0, "1": 1, "2": 2, "3": 3}
	for _, row := range rows {
		id, _ := row["id"].(string)
		if row["chunk_count"] != want[id] || row["document_id"] != "document" || len(row) != 11 {
			t.Fatalf("incorrect task summary: %+v", row)
		}
	}
	if snapshot() != before {
		t.Fatal("task listing mutated committed rows")
	}
	if err := db.Where("1=1").Delete(&entity.Task{}).Error; err != nil {
		t.Fatal(err)
	}
	result, err = client.ExecuteCommand(command)
	if err != nil {
		t.Fatal(err)
	}
	if result.(*cli.CommonResponse).Data == nil || len(result.(*cli.CommonResponse).Data) != 0 {
		t.Fatal("empty task list must be []")
	}
	if err := db.Migrator().DropTable(&entity.Task{}); err != nil {
		t.Fatal(err)
	}
	request, _ := http.NewRequest(http.MethodGet, httpServer.URL+"/api/v1/admin/tasks", nil)
	request.Header.Set("Authorization", tokens["admin"])
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	decoder := json.NewDecoder(response.Body)
	var failure ErrorResponse
	if err := decoder.Decode(&failure); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != 500 || failure.Code != int(common.CodeServerError) {
		t.Fatalf("DAO failure masked: %d %+v", response.StatusCode, failure)
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		t.Fatal("error response followed by success", extra, err)
	}
	if _, err := client.ExecuteCommand(command); err == nil {
		t.Fatal("CLI accepted database failure")
	}
}
