package models

import (
	"context"
	"errors"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
	"io"
	"testing"
	"time"
)

type einoProbeDriver struct {
	DummyModel
	started chan struct{}
	stopped chan struct{}
	config  chan ChatConfig
}

func (d *einoProbeDriver) ChatWithMessages(_ string, api *APIConfig, messages []Message, cfg *ChatConfig) (*ChatResponse, error) {
	if err := api.Context.Err(); err != nil {
		return nil, err
	}
	d.config <- *cfg
	if len(messages) != 2 || messages[0].Role != "system" || messages[1].Role != "user" {
		return nil, errors.New("roles dropped")
	}
	answer, reason := "answer", "reason"
	return &ChatResponse{Answer: &answer, ReasoningContent: &reason}, nil
}
func (d *einoProbeDriver) ChatStreamlyWithMessages(_ string, _ []Message, api *APIConfig, _ *ChatConfig, send func(*string, *string) error) error {
	close(d.started)
	defer close(d.stopped)
	<-api.Context.Done()
	return api.Context.Err()
}
func TestEinoAdapterOptionsAndUnsupportedTools(t *testing.T) {
	driver := &einoProbeDriver{config: make(chan ChatConfig, 1)}
	name := "authorized"
	cm := NewChatModel(driver, &name, &APIConfig{})
	adapter := NewEinoChatModel(cm, nil)
	msgs := []*schema.Message{{Role: schema.System, Content: "system"}, {Role: schema.User, Content: "hello"}}
	response, err := adapter.Generate(context.Background(), msgs, model.WithTemperature(0), model.WithTopP(0), model.WithMaxTokens(12))
	if err != nil || response.Content != "answer" || response.ReasoningContent != "reason" {
		t.Fatal(response, err)
	}
	cfg := <-driver.config
	if cfg.Temperature == nil || *cfg.Temperature != 0 || cfg.TopP == nil || *cfg.TopP != 0 || cfg.MaxTokens == nil || *cfg.MaxTokens != 12 {
		t.Fatal("options lost", cfg)
	}
	if cm.APIConfig.Context != nil || cm.ModelConfig.Temperature != nil {
		t.Fatal("shared state mutated")
	}
	if _, err := adapter.WithTools([]*schema.ToolInfo{{Name: "tool"}}); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal("tool no-op accepted", err)
	}
	if _, err := adapter.Generate(context.Background(), msgs, model.WithModel("other")); err == nil {
		t.Fatal("authorized model overridden")
	}
	if _, err := adapter.Generate(context.Background(), []*schema.Message{{Role: schema.Tool, Content: "tool"}}); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := adapter.Generate(ctx, msgs); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
func TestEinoAdapterStreamCloseCancelsStalledProvider(t *testing.T) {
	driver := &einoProbeDriver{started: make(chan struct{}), stopped: make(chan struct{})}
	name := "authorized"
	adapter := NewEinoChatModel(NewChatModel(driver, &name, &APIConfig{}), nil)
	reader, err := adapter.Stream(context.Background(), []*schema.Message{{Role: schema.User, Content: "hello"}})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-driver.started:
	case <-time.After(time.Second):
		t.Fatal("provider did not start")
	}
	reader.Close()
	select {
	case <-driver.stopped:
	case <-time.After(time.Second):
		t.Fatal("reader close leaked stalled provider")
	}
}

type einoCompletedDriver struct{ DummyModel }

func (d *einoCompletedDriver) ChatStreamlyWithMessages(_ string, _ []Message, _ *APIConfig, _ *ChatConfig, send func(*string, *string) error) error {
	answer, done := "answer", "[DONE]"
	if err := send(&answer, nil); err != nil {
		return err
	}
	return send(&done, nil)
}
func TestEinoAdapterStreamTerminal(t *testing.T) {
	name := "model"
	adapter := NewEinoChatModel(NewChatModel(&einoCompletedDriver{}, &name, &APIConfig{}), nil)
	reader, err := adapter.Stream(context.Background(), []*schema.Message{{Role: schema.User, Content: "hello"}})
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	first, err := reader.Recv()
	if err != nil || first.Content != "answer" {
		t.Fatal(first, err)
	}
	if _, err := reader.Recv(); !errors.Is(err, io.EOF) {
		t.Fatal("terminal leaked", err)
	}
}
