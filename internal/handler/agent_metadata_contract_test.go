package handler

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"
)

func TestAgentMetadataStrictRequestsBeforeSQL(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		name     string
		action   func(*gin.Context)
		payloads []string
	}{
		{"delete", NewAgentHandler(service.NewAgentService()).DeleteAgentSessions, []string{"", `null`, `[]`, `{"ids":null}`, `{"ids":[null]}`, `{"ids":[1]}`, `{"ids":[""]}`, `{"delete_all":null}`, `{"delete_all":1}`, `{"delete_all":"true"}`, `{"ids":[],"ids":["a"]}`, `{"ids":[],"IDS":["a"]}`, `{"DeleteAll":true}`, `{"unknown":true}`, `{} {}`}},
		{"metadata", NewDatasetsHandler(service.NewDatasetsService()).UpdateDocumentMetadataConfig, []string{"", `null`, `[]`, `{}`, `{"metadata":null}`, `{"metadata":false}`, `{"metadata":1}`, `{"Metadata":[]}`, `{"metadata":[],"enabled":true}`, `{"metadata":[],"metadata":{}}`, `{"metadata":[]} {}`}},
		{"probe", NewAgentHandler(service.NewAgentService()).TestDBConnection, []string{"", `null`, `[]`, `{"password":null}`, `{"host":1}`, `{"db_type":"sqlite"}`, `{"db_type":"mysql"}`, `{"db_type":"mysql","sql":"DROP TABLE x"}`, `{"db_type":"mysql","url":"mysql://secret"}`, `{"port":3306,"Port":3307}`, `{"port":3306,"port":3307}`, `{} {}`}},
	} {
		for _, payload := range tc.payloads {
			t.Run(tc.name+"/"+payload, func(t *testing.T) {
				w := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(w)
				c.Set("user", &entity.User{ID: "owner"})
				c.Params = gin.Params{{Key: "agent_id", Value: "agent"}, {Key: "dataset_id", Value: "00000000000010008000000000000001"}, {Key: "document_id", Value: "doc"}}
				c.Request = httptest.NewRequest("POST", "/", strings.NewReader(payload))
				tc.action(c)
				var body struct {
					Code common.ErrorCode `json:"code"`
				}
				if json.Unmarshal(w.Body.Bytes(), &body) != nil || body.Code != common.CodeArgumentError {
					t.Fatal(w.Body.String())
				}
			})
		}
	}
}
