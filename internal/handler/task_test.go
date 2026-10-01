package handler

import (
	"bytes"
	"encoding/json"
	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"net/http/httptest"
	"testing"
)

func TestTaskPatchBodyAndID(t *testing.T) {
	for _, body := range []string{`{}`, `{"action":"start"}`, `{"action":null}`, `{"action":"stop","user_id":"fake"}`, `{"action":"stop"} {}`} {
		writer := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(writer)
		c.Request = httptest.NewRequest("PATCH", "/api/v1/tasks/task", bytes.NewBufferString(body))
		NewTaskHandler(nil).Update(c)
		var response struct{ Code int }
		if err := json.Unmarshal(writer.Body.Bytes(), &response); err != nil || response.Code != 110 {
			t.Fatalf("invalid body accepted: %s", body)
		}
	}
	writer := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(writer)
	c.Request = httptest.NewRequest("POST", "/api/v1/tasks/bad/cancel", nil)
	c.Set("user", &entity.User{ID: "owner"})
	c.Params = gin.Params{{Key: "task_id", Value: "bad id"}}
	NewTaskHandler(nil).Cancel(c)
	var response struct{ Code int }
	if err := json.Unmarshal(writer.Body.Bytes(), &response); err != nil || response.Code != 110 {
		t.Fatal("invalid task id accepted")
	}
}
