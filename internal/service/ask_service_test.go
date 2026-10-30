package service

import (
	"context"
	"errors"
	"strings"
	"testing"

	"multirag/internal/entity/models"
)

type answerTestDelta struct{ content, reasoning string }
type answerTestDriver struct {
	models.ModelDriver
	deltas   []answerTestDelta
	failure  error
	consumed int
}

func (d *answerTestDriver) ChatStreamlyWithMessages(_ string, _ []models.Message, _ *models.APIConfig, cfg *models.ChatConfig, send func(*string, *string) error) error {
	for _, delta := range d.deltas {
		d.consumed++
		a, b := delta.content, delta.reasoning
		if err := send(&a, &b); err != nil {
			return err
		}
	}
	if d.failure != nil {
		return d.failure
	}
	done := "[DONE]"
	return send(&done, nil)
}

func TestAfter10bbeVisibleStreamFailureAndReasoning(t *testing.T) {
	name := "fixture"
	for _, tc := range []struct {
		name     string
		deltas   []answerTestDelta
		failure  error
		want     string
		failSend bool
		cancel   bool
		ok       bool
	}{
		{name: "split", deltas: []answerTestDelta{{"<thi", "typed "}, {"nk>private</th", ""}, {"ink>Visible ", ""}, {"answer [ID:one]", ""}}, want: "Visible answer [ID:one]", ok: true},
		{name: "unclosed", deltas: []answerTestDelta{{"Visible <think>private", ""}}, want: "Visible ", ok: true},
		{name: "private-only", deltas: []answerTestDelta{{"<think>private", ""}}},
		{name: "typed-only", deltas: []answerTestDelta{{"", "private"}}},
		{name: "provider-error", deltas: []answerTestDelta{{"partial", ""}}, failure: errors.New("provider failed")},
		{name: "sender-error", deltas: []answerTestDelta{{"partial", ""}, {"never", ""}}, failSend: true},
		{name: "cancel", deltas: []answerTestDelta{{"partial", ""}, {"never", ""}}, cancel: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			driver := &answerTestDriver{deltas: tc.deltas, failure: tc.failure}
			model := models.NewChatModel(driver, &name, &models.APIConfig{})
			frames := []AnswerFrame{}
			answer, reasoning, err := streamVisibleAnswer(ctx, model, []models.Message{{Role: "user", Content: "question"}}, map[string]interface{}{}, []SourcedChunk{{ID: "one"}}, true, func(frame AnswerFrame) error {
				if strings.Contains(frame.Answer, "private") || strings.Contains(frame.Answer, "<thi") || frame.Final {
					t.Fatal("private content or premature final", frame)
				}
				frames = append(frames, frame)
				if tc.cancel {
					cancel()
				}
				if tc.failSend {
					return errors.New("sender failed")
				}
				return nil
			})
			if tc.ok {
				if err != nil || answer != tc.want {
					t.Fatal(answer, reasoning, err)
				}
			} else if err == nil {
				t.Fatal("failed stream reported success")
			}
			if (tc.cancel || tc.failSend) && driver.consumed != 1 {
				t.Fatal("producer continued after termination")
			}
			if model.APIConfig.Context != nil {
				t.Fatal("shared model config mutated")
			}
			if tc.name == "split" && reasoning != "typed private" {
				t.Fatal(reasoning)
			}
		})
	}
}

func TestAfter10bbeAskRejectsActorAndInputBeforeDependencies(t *testing.T) {
	svc := NewAskService(nil, nil)
	if err := svc.Stream(context.Background(), "", &AskRequest{Question: "q", KBIDs: []string{"kb"}}, nil); !errors.Is(err, ErrSearchbotAccessDenied) {
		t.Fatal(err)
	}
	for _, req := range []*AskRequest{nil, {Question: "q"}, {Question: " ", KBIDs: []string{"kb"}}, {Question: "q", KBIDs: []string{" kb"}}, {Question: "q", KBIDs: []string{"kb"}, SearchID: " search"}} {
		if err := svc.Stream(context.Background(), "actor", req, nil); !errors.Is(err, ErrRetrievalArgument) {
			t.Fatal(err)
		}
	}
}
