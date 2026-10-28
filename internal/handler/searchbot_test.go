package handler

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"
)

type fakeRelatedQuestions struct {
	calls                      int
	ctx                        context.Context
	userID, question, searchID string
	result                     []string
	err                        error
}

func (f *fakeRelatedQuestions) RelatedQuestions(ctx context.Context, userID, question, searchID string) ([]string, error) {
	f.calls++
	f.ctx, f.userID, f.question, f.searchID = ctx, userID, question, searchID
	return f.result, f.err
}

func TestSearchbotRejectsInvalidWithoutServiceCall(t *testing.T) {
	for _, body := range []string{`{}`, `null`, `[]`, `{"question":null}`, `{"question":7}`, `{"question":" "}`, `{"question":"q","search_id":null}`, `{"question":"q","search_id":3}`, `{"question":"q","search_id":" "}`, `{"question":"q","extra":true}`, `{"question":"q","question":"second"}`, `{"question":"q"} {}`} {
		t.Run(body, func(t *testing.T) {
			fake := &fakeRelatedQuestions{}
			c, w := searchbotContext(body)
			NewSearchbotHandler(fake).Handle(c)
			if got := searchbotCode(t, w); got != common.CodeArgumentError || fake.calls != 0 {
				t.Fatalf("code=%d service calls=%d", got, fake.calls)
			}
		})
	}
	fake := &fakeRelatedQuestions{}
	c, w := searchbotContext(`{"question":"q"}`)
	c.Set("user", (*entity.User)(nil))
	NewSearchbotHandler(fake).Handle(c)
	if searchbotCode(t, w) != common.CodeUnauthorized || fake.calls != 0 {
		t.Fatal("unauthenticated call reached service")
	}
}

func TestSearchbotUsesRequestContextAndSanitizesErrors(t *testing.T) {
	for _, tc := range []struct {
		err  error
		code common.ErrorCode
	}{
		{nil, common.CodeSuccess}, {service.ErrSearchbotArgument, common.CodeArgumentError}, {service.ErrSearchbotAccessDenied, common.CodeAuthenticationError}, {errors.New("private-key-provider-body"), common.CodeOperatingError}, {context.Canceled, common.CodeOperatingError},
	} {
		fake := &fakeRelatedQuestions{err: tc.err}
		c, w := searchbotContext(`{"question":"question","search_id":"search"}`)
		ctx, cancel := context.WithCancel(c.Request.Context())
		defer cancel()
		c.Request = c.Request.WithContext(ctx)
		NewSearchbotHandler(fake).Handle(c)
		if searchbotCode(t, w) != tc.code || fake.calls != 1 || fake.ctx != ctx || fake.userID != "actor" || fake.question != "question" || fake.searchID != "search" {
			t.Fatalf("incorrect dispatch: %s", w.Body.String())
		}
		if strings.Contains(w.Body.String(), "private-key") {
			t.Fatal("error leaked provider data")
		}
		if tc.err == nil && !strings.Contains(w.Body.String(), `"data":[]`) {
			t.Fatal("empty questions must encode []")
		}
	}
}

func searchbotContext(body string) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("POST", "/api/v1/searchbots/related_questions", strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set("user", &entity.User{ID: "actor"})
	return c, w
}

func searchbotCode(t *testing.T, w *httptest.ResponseRecorder) common.ErrorCode {
	t.Helper()
	var response struct {
		Code common.ErrorCode `json:"code"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	return response.Code
}
