package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/compose"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"multirag/internal/agent/canvas"
	"multirag/internal/agent/component"
	"multirag/internal/common"
	"multirag/internal/dao"
	redisengine "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

const agentExecutionTTL = 24 * time.Hour
const agentExecutionTimeout = 5 * time.Minute

// Final status, cancellation, checkpoint disposal and lease release share one
// Redis linearization point. JSON payloads are encoded in Go with UseNumber;
// Lua never re-encodes DSL values through cjson's floating-point numbers.
const agentFinalizeScript = `-- multirag.agent.finalize
if redis.call('GET',KEYS[3]) ~= ARGV[1] then return redis.error_reply('agent run lease lost') end
local status = ARGV[4]
local payload = ARGV[2]
if redis.call('EXISTS',KEYS[2]) > 0 then status = 'cancelled'; payload = ARGV[3] end
redis.call('SET',KEYS[1],payload,'PX',ARGV[5])
if status ~= 'waiting' then redis.call('DEL',KEYS[4]) end
redis.call('DEL',KEYS[3])
return status`

const agentCancelScript = `-- multirag.agent.cancel
local raw = redis.call('GET',KEYS[1])
if not raw then return 'missing' end
local record = cjson.decode(raw)
if record.actor_id ~= ARGV[1] or record.canvas_id ~= ARGV[2] then return 'missing' end
if record.status ~= 'waiting' and record.status ~= 'running' then return record.status end
redis.call('SET',KEYS[2],'1','PX',ARGV[4])
if redis.call('EXISTS',KEYS[3]) > 0 then return 'active' end
redis.call('SET',KEYS[1],ARGV[3],'PX',ARGV[4])
redis.call('DEL',KEYS[4])
return 'cancelled'`

type agentRedisProvider func() (*redis.Client, error)
type agentModelResolver func(context.Context, string, string) (model.ToolCallingChatModel, error)

func agentRedisClient() (*redis.Client, error) {
	if rc := redisengine.Get(); rc != nil && rc.GetClient() != nil {
		return rc.GetClient(), nil
	}
	return nil, fmt.Errorf("Agent execution requires Redis.")
}

func resolveAgentModel(ctx context.Context, tenantID, modelID string) (model.ToolCallingChatModel, error) {
	provider := NewModelProviderService()
	var bound *models.ChatModel
	var err error
	if modelID == "" {
		bound, err = provider.GetChatModelWithContext(ctx, tenantID, "")
	} else {
		id, parseErr := strconv.ParseInt(modelID, 10, 64)
		if parseErr != nil || id <= 0 {
			return nil, fmt.Errorf("model ID must be a positive tenant model ID")
		}
		bound, err = provider.GetChatModelByIDWithContext(ctx, tenantID, id)
	}
	if err != nil {
		return nil, fmt.Errorf("Unable to resolve authorized agent model.")
	}
	return models.NewEinoChatModel(bound, nil), nil
}

// AgentRunRequest is the explicit Go execution API, independent of Python sessions.
// Resume uses the immutable stored DSL and accepts exactly one interrupt target.
type AgentRunRequest struct {
	Query       string         `json:"query,omitempty"`
	Inputs      map[string]any `json:"inputs,omitempty"`
	VersionID   string         `json:"version_id,omitempty"`
	RunID       string         `json:"run_id,omitempty"`
	InterruptID string         `json:"interrupt_id,omitempty"`
	Resume      any            `json:"resume,omitempty"`
}

type AgentRunEvent struct {
	Event string         `json:"event"`
	RunID string         `json:"run_id"`
	Data  map[string]any `json:"data,omitempty"`
}

type agentExecutionRecord struct {
	ActorID      string          `json:"actor_id"`
	CanvasID     string          `json:"canvas_id"`
	TenantID     string          `json:"tenant_id"`
	DSL          json.RawMessage `json:"dsl"`
	Input        map[string]any  `json:"input"`
	Status       string          `json:"status"`
	InterruptIDs []string        `json:"interrupt_ids,omitempty"`
}

var agentRunIDPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)

func agentRecordKey(runID string) string { return "agent:execution:" + runID }

func readAgentExecution(ctx context.Context, client *redis.Client, runID, actorID, canvasID string) (*agentExecutionRecord, error) {
	raw, err := client.Get(ctx, agentRecordKey(runID)).Bytes()
	if err != nil {
		return nil, err
	}
	var record agentExecutionRecord
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	if err := d.Decode(&record); err != nil {
		return nil, err
	}
	if record.ActorID != actorID || record.CanvasID != canvasID {
		return nil, redis.Nil
	}
	return &record, nil
}

func saveAgentExecution(ctx context.Context, client *redis.Client, runID string, record *agentExecutionRecord) error {
	raw, err := json.Marshal(record)
	if err != nil {
		return err
	}
	return client.Set(ctx, agentRecordKey(runID), raw, agentExecutionTTL).Err()
}

func agentExecutionKeys(runID string) []string {
	return []string{agentRecordKey(runID), agentRecordKey(runID) + ":cancel", agentRecordKey(runID) + ":lock", "agent:cp:" + runID}
}

func finalizeAgentExecution(ctx context.Context, client *redis.Client, runID, token string, record *agentExecutionRecord) (string, error) {
	raw, err := json.Marshal(record)
	if err != nil {
		return "", err
	}
	cancelled := *record
	cancelled.Status = "cancelled"
	cancelled.InterruptIDs = nil
	cancelledRaw, err := json.Marshal(cancelled)
	if err != nil {
		return "", err
	}
	return client.Eval(ctx, agentFinalizeScript, agentExecutionKeys(runID), token, raw, cancelledRaw, record.Status, agentExecutionTTL.Milliseconds()).Text()
}

func releaseAgentExecution(ctx context.Context, client *redis.Client, runID, token string) error {
	return client.Eval(ctx, `if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end`, []string{agentRecordKey(runID) + ":lock"}, token).Err()
}

func decodeExecutableCanvas(raw json.RawMessage) (*canvas.Canvas, error) {
	var graph canvas.Canvas
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	if err := d.Decode(&graph); err != nil || len(graph.Components) == 0 || len(graph.Components) > 256 {
		return nil, fmt.Errorf("Agent DSL must contain 1 to 256 v1 components.")
	}
	if err := d.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("Agent DSL must contain one JSON object.")
	}
	if graph.Version > 1 {
		return nil, fmt.Errorf("Go execution currently accepts v1 canvas DSL.")
	}
	return &graph, nil
}

// RunAgent authorizes SQL access before touching execution metadata or models.
// The Redis lease serializes resume across processes; each invocation is bounded.
func (s *AgentService) RunAgent(ctx context.Context, actorID, canvasID string, req *AgentRunRequest) (<-chan AgentRunEvent, common.ErrorCode, error) {
	row, code, err := s.GetAgent(ctx, actorID, canvasID)
	if err != nil {
		return nil, code, err
	}
	if req == nil {
		return nil, common.CodeArgumentError, fmt.Errorf("Run request is required.")
	}
	if len(req.Query) > 1<<20 {
		return nil, common.CodeArgumentError, fmt.Errorf("Query is too large.")
	}
	if req.RunID != "" && !agentRunIDPattern.MatchString(req.RunID) {
		return nil, common.CodeArgumentError, fmt.Errorf("Invalid run_id.")
	}
	if s.runClient == nil || s.modelResolver == nil {
		return nil, common.CodeNotImplemented, fmt.Errorf("Agent execution is not configured.")
	}
	client, err := s.runClient()
	if err != nil || client == nil {
		return nil, common.CodeConnectionError, fmt.Errorf("Agent execution requires Redis.")
	}
	runID := req.RunID
	var record *agentExecutionRecord
	if runID == "" {
		if req.InterruptID != "" || req.Resume != nil {
			return nil, common.CodeArgumentError, fmt.Errorf("Resume requires run_id and interrupt_id.")
		}
		runID = s.runID()
		rawDSL := row.DSL
		if req.VersionID != "" {
			var version struct{ DSL json.RawMessage }
			if err := dao.DB.WithContext(ctx).Model(&entity.UserCanvasVersion{}).Select("dsl").Where("id = ? AND user_canvas_id = ?", req.VersionID, canvasID).First(&version).Error; err != nil {
				return nil, common.CodeNotFound, fmt.Errorf("Agent version not found.")
			}
			rawDSL = version.DSL
		}
		input := map[string]any{}
		for k, v := range req.Inputs {
			if k == "state" || strings.HasPrefix(k, "__") || k == "user_id" {
				return nil, common.CodeArgumentError, fmt.Errorf("Reserved input field.")
			}
			input[k] = v
		}
		input["query"] = req.Query
		input["user_id"] = actorID
		record = &agentExecutionRecord{ActorID: actorID, CanvasID: canvasID, TenantID: row.UserID, DSL: rawDSL, Input: input, Status: "running"}
	} else {
		if req.InterruptID == "" || req.Resume == nil || req.VersionID != "" || req.Query != "" || len(req.Inputs) > 0 {
			return nil, common.CodeArgumentError, fmt.Errorf("Resume accepts run_id, interrupt_id and resume data only.")
		}
		record, err = readAgentExecution(ctx, client, runID, actorID, canvasID)
		if errors.Is(err, redis.Nil) {
			return nil, common.CodeNotFound, fmt.Errorf("Agent run not found.")
		}
		if err != nil {
			return nil, common.CodeConnectionError, fmt.Errorf("Unable to read agent run.")
		}
	}
	graph, err := decodeExecutableCanvas(record.DSL)
	if err != nil {
		return nil, common.CodeArgumentError, err
	}
	runCtx, cancel := context.WithTimeout(ctx, agentExecutionTimeout)
	runCtx = component.WithModelResolver(runCtx, func(callCtx context.Context, id string) (model.ToolCallingChatModel, error) {
		return s.modelResolver(callCtx, record.TenantID, id)
	})
	state := canvas.NewCanvasState(runID, runID)
	state.SetSys("user_id", actorID)
	state.SetSys("query", record.Input["query"])
	state.Path = graph.Path
	state.History = graph.History
	state.Retrieval = graph.Retrieval
	state.Globals = graph.Globals
	if state.Globals == nil {
		state.Globals = map[string]any{}
	}
	runCtx = canvas.WithState(runCtx, state)
	store := canvas.NewRedisCheckPointStoreWithClient(client, agentExecutionTTL)
	compiled, err := canvas.Compile(runCtx, graph, canvas.WithCheckPointStore(store))
	if err != nil {
		cancel()
		return nil, common.CodeNotImplemented, fmt.Errorf("Agent DSL cannot execute: %w", err)
	}
	token := uuid.NewString()
	claimed, err := client.SetNX(ctx, agentRecordKey(runID)+":lock", token, agentExecutionTimeout+time.Minute).Result()
	if err != nil || !claimed {
		cancel()
		return nil, common.CodeConflict, fmt.Errorf("Agent run is already active or unavailable.")
	}
	fail := func(code common.ErrorCode, message string) (<-chan AgentRunEvent, common.ErrorCode, error) {
		cancel()
		_ = releaseAgentExecution(ctx, client, runID, token)
		return nil, code, errors.New(message)
	}
	if req.RunID != "" {
		record, err = readAgentExecution(ctx, client, runID, actorID, canvasID)
		if err != nil || record.Status != "waiting" || len(record.InterruptIDs) != 1 || record.InterruptIDs[0] != req.InterruptID {
			return fail(common.CodeConflict, "Agent run is not waiting for this interrupt.")
		}
		runCtx = compose.ResumeWithData(runCtx, req.InterruptID, req.Resume)
	}
	if requested, e := client.Exists(ctx, agentRecordKey(runID)+":cancel").Result(); e != nil || requested > 0 {
		return fail(common.CodeConflict, "Agent run was cancelled or is unavailable.")
	}
	record.Status = "running"
	record.InterruptIDs = nil
	if err = saveAgentExecution(ctx, client, runID, record); err != nil {
		return fail(common.CodeConnectionError, "Unable to persist agent run.")
	}
	out := make(chan AgentRunEvent, 8)
	go func() {
		defer close(out)
		defer cancel()
		defer func() {
			cleanup, done := context.WithTimeout(context.Background(), 5*time.Second)
			defer done()
			_ = releaseAgentExecution(cleanup, client, runID, token)
		}()
		emit := func(event string, data map[string]any) {
			timer := time.NewTimer(time.Second)
			defer timer.Stop()
			select {
			case out <- AgentRunEvent{Event: event, RunID: runID, Data: data}:
			case <-ctx.Done():
			case <-timer.C:
				cancel()
			}
		}
		watchDone := make(chan struct{})
		go func() {
			defer close(watchDone)
			ticker := time.NewTicker(100 * time.Millisecond)
			defer ticker.Stop()
			for {
				select {
				case <-runCtx.Done():
					return
				case <-ticker.C:
					requested, e := client.Exists(runCtx, agentRecordKey(runID)+":cancel").Result()
					if e != nil || requested > 0 {
						cancel()
						return
					}
				}
			}
		}()
		emit("workflow_started", nil)
		input := record.Input
		if req.RunID != "" {
			input = nil
		}
		_, runErr := compiled.Workflow.Invoke(runCtx, input, compose.WithCheckPointID(runID))
		data := map[string]any{}
		event := "workflow_finished"
		messages := []map[string]any{}
		switch {
		case canvas.IsInterruptError(runErr):
			contexts := canvas.ExtractInterruptContexts(runErr)
			if len(contexts) == 1 && contexts[0] != nil {
				record.Status = "waiting"
				record.InterruptIDs = []string{contexts[0].ID}
				event = "waiting_for_user"
				data["interrupt_id"] = contexts[0].ID
				data["input_spec"] = contexts[0].Info
			} else {
				record.Status = "failed"
				event = "error"
				data["message"] = "Multiple interrupts are not supported by this execution API."
			}
		case runErr != nil:
			record.Status = "failed"
			event = "error"
			data["message"] = "Agent execution failed."
			if errors.Is(runErr, context.Canceled) || errors.Is(runErr, context.DeadlineExceeded) {
				record.Status = "cancelled"
				event = "cancelled"
			}
		default:
			record.Status = "succeeded"
			terminals := []string{}
			for id, node := range graph.Components {
				if len(node.Downstream) == 0 && strings.EqualFold(node.Obj.ComponentName, "Message") {
					terminals = append(terminals, id)
				}
			}
			sort.Strings(terminals)
			for _, id := range terminals {
				if value, err := state.GetVar(id + "@content"); err == nil {
					if content, ok := value.(string); ok {
						messages = append(messages, map[string]any{"content": content, "component_id": id})
					}
				}
			}
		}
		persistCtx, done := context.WithTimeout(context.Background(), 5*time.Second)
		status, err := finalizeAgentExecution(persistCtx, client, runID, token, record)
		if err != nil {
			event = "error"
			data = map[string]any{"message": "Unable to persist execution status."}
		} else if status == "cancelled" {
			event = "cancelled"
			data = nil
		} else if status == "succeeded" {
			for _, message := range messages {
				emit("message", message)
			}
		}
		done()
		emit(event, data)
		cancel()
		<-watchDone
	}()
	return out, common.CodeSuccess, nil
}

func (s *AgentService) CancelAgent(ctx context.Context, actorID, canvasID, runID string) (common.ErrorCode, error) {
	if _, code, err := s.requireCanvasAccess(ctx, actorID, canvasID); err != nil {
		return code, err
	}
	if !agentRunIDPattern.MatchString(runID) {
		return common.CodeArgumentError, fmt.Errorf("Invalid run_id.")
	}
	if s.runClient == nil {
		return common.CodeNotImplemented, fmt.Errorf("Agent execution is not configured.")
	}
	client, err := s.runClient()
	if err != nil || client == nil {
		return common.CodeConnectionError, fmt.Errorf("Agent execution requires Redis.")
	}
	record, err := readAgentExecution(ctx, client, runID, actorID, canvasID)
	if errors.Is(err, redis.Nil) {
		return common.CodeNotFound, fmt.Errorf("Agent run not found.")
	}
	if err != nil {
		return common.CodeConnectionError, fmt.Errorf("Unable to read agent run.")
	}
	record.Status = "cancelled"
	record.InterruptIDs = nil
	raw, err := json.Marshal(record)
	if err != nil {
		return common.CodeServerError, fmt.Errorf("Unable to encode cancellation.")
	}
	status, err := client.Eval(ctx, agentCancelScript, agentExecutionKeys(runID), actorID, canvasID, raw, agentExecutionTTL.Milliseconds()).Text()
	if err != nil {
		return common.CodeConnectionError, fmt.Errorf("Unable to cancel agent run.")
	}
	if status == "missing" {
		return common.CodeNotFound, fmt.Errorf("Agent run not found.")
	}
	return common.CodeSuccess, nil
}
