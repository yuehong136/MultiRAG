package handler

import (
	"fmt"
	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/service"
	"multirag/internal/testsupport/scratch"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestSearchbotParseQuestions(t *testing.T) {
	for _, test := range []struct {
		input string
		want  []string
	}{
		{"", []string{}}, {"no suggestions", []string{}},
		{"  1. first  \n10. tenth\nnot numbered\n2. ", []string{"first", "tenth"}},
		{"<think>\n1. hidden forever", []string{}},
	} {
		if got := parseRelatedQuestions(test.input); !reflect.DeepEqual(got, test.want) {
			t.Errorf("parse %q = %v, want %v", test.input, got, test.want)
		}
	}
}

type canonicalRelatedLLM struct {
	calls         int
	tenant, model string
}

func (f *canonicalRelatedLLM) Chat(tenant, model string, _ []models.Message, configuration *models.ChatConfig) (*models.ChatResponse, error) {
	f.calls++
	f.tenant, f.model = tenant, model
	answer := "<think>1. private reasoning</think>\n1. First\n10. Tenth"
	if configuration.Temperature == nil || *configuration.Temperature != 0.9 {
		return nil, fmt.Errorf("frozen generation default changed")
	}
	return &models.ChatResponse{Answer: &answer}, nil
}
func TestCanonicalSearchbotSQLScopeBeforeLLM(t *testing.T) {
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Search{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	owner, member, invite, other := f.Prefix+"owner", f.Prefix+"member", f.Prefix+"invite", f.Prefix+"other"
	for _, id := range []string{owner, member, invite, other} {
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@example.test", Nickname: id, IsActive: "1", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.Tenant{ID: owner, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	for _, relation := range []entity.UserTenant{{ID: owner, UserID: owner, TenantID: owner, Role: "owner", Status: &active}, {ID: member, UserID: member, TenantID: owner, Role: "normal", Status: &active}, {ID: invite, UserID: invite, TenantID: owner, Role: "invite", Status: &active}} {
		if err := f.DB.Create(&relation).Error; err != nil {
			t.Fatal(err)
		}
	}
	search := f.Prefix + "search"
	if err := f.DB.Create(&entity.Search{ID: search, TenantID: owner, CreatedBy: owner, Name: "Search", SearchConfig: entity.JSONMap{"chat_id": "chosen@instance@provider"}, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	llm := &canonicalRelatedLLM{}
	h := NewSearchBotHandler(service.NewSearchService(), nil, llm, nil)
	for _, actor := range []string{owner, member, invite, other, "missing"} {
		before := llm.calls
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Set("user", &entity.User{ID: actor})
		c.Request = httptest.NewRequest("POST", "/api/v1/searchbots/related_questions", strings.NewReader(fmt.Sprintf(`{"question":"q","search_id":%q}`, search)))
		c.Request.Header.Set("Content-Type", "application/json")
		h.Handle(c)
		if actor == owner || actor == member {
			if llm.calls != before+1 || llm.model != "chosen@instance@provider" || llm.tenant != actor {
				t.Fatal("canonical selected model/actor lost", llm)
			}
		} else if llm.calls != before {
			t.Fatal("unauthorized search invoked LLM", actor)
		}
	}
	if err := f.DB.Model(&entity.User{}).Where("id=?", owner).Update("is_active", "0").Error; err != nil {
		t.Fatal(err)
	}
	before := llm.calls
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Set("user", &entity.User{ID: owner})
	c.Request = httptest.NewRequest("POST", "/api/v1/searchbots/related_questions", strings.NewReader(fmt.Sprintf(`{"question":"q","search_id":%q}`, search)))
	c.Request.Header.Set("Content-Type", "application/json")
	h.Handle(c)
	if llm.calls != before {
		t.Fatal("inactive actor reached LLM")
	}
}
