package service

import (
	"encoding/json"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/utility"
)

func TestHeartbeatResponseValidationAndRetry(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		valid  bool
	}{
		{"valid", 200, `{"code":320}`, true},
		{"missing", 500, `{}`, false}, {"null", 500, `{"code":null}`, false},
		{"string", 500, `{"code":"320"}`, false}, {"fraction", 500, `{"code":320.5}`, false},
		{"bool", 500, `{"code":false}`, false}, {"overflow", 500, `{"code":99999999999999999999999999}`, false},
		{"malformed", 500, `broken`, false}, {"wrong-code", 500, `{"code":401}`, false},
		{"wrong-status", 503, `{"code":320}`, false}, {"false-success", 200, `{"code":401}`, false},
		{"empty-success", 200, `{}`, false},
		{"trailing-json", 200, `{"code":320}{"code":401}`, false},
		{"trailing-garbage", 200, `{"code":320}oops`, false},
		{"oversize", 200, strings.Repeat(" ", maxHeartbeatResponseBytes+1) + `{"code":320}`, false},
		{"valid-whitespace", 200, " \n" + `{"code":320}` + "\t", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int64
			fixture := registeredModelServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Method != "POST" || r.URL.Path != "/api/v1/admin/reports" {
					t.Error("heartbeat request contract changed")
				}
				var message common.BaseMessage
				if err := json.NewDecoder(r.Body).Decode(&message); err != nil || message.MessageType != common.MessageHeartbeat || message.ServerName != "owned-api" {
					t.Error("invalid heartbeat payload")
				}
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			address, _ := url.Parse(fixture.URL)
			host, portText, _ := net.SplitHostPort(address.Host)
			port, _ := strconv.Atoi(portText)
			h := NewHeartbeatSender(nil, common.ServerTypeAPI, "owned-api", "127.0.0.1", 9380)
			h.client = utility.NewHTTPClientBuilder().WithHost(host).WithPort(port).WithTimeout(time.Second).Build()
			err := h.SendHeartbeat()
			if (err == nil) != tc.valid || h.lastSuccess != tc.valid {
				t.Fatalf("valid=%v success=%v err=%v", tc.valid, h.lastSuccess, err)
			}
			if err != nil && strings.Contains(err.Error(), tc.body) {
				t.Fatal("raw remote response exposed")
			}
			if err = h.SendHeartbeat(); (err == nil) != tc.valid {
				t.Fatalf("retry changed: %v", err)
			}
			want := 2
			if tc.valid {
				want = 1
			}
			if got := calls.Load(); got != int64(want) {
				t.Fatalf("retry/throttle requests=%d want=%d", got, want)
			}
		})
	}
}

func TestHeartbeatNilSenderFailsWithoutPanic(t *testing.T) {
	var h *HeartbeatSender
	if h.SendHeartbeat() == nil || h.InitHTTPClient() == nil {
		t.Fatal("nil sender reported success")
	}
}

func TestHeartbeatTransportFailureRetries(t *testing.T) {
	var calls atomic.Int64
	fixture := registeredModelServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		connection, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		_ = connection.Close()
	}))
	address, _ := url.Parse(fixture.URL)
	host, portText, _ := net.SplitHostPort(address.Host)
	port, _ := strconv.Atoi(portText)
	h := NewHeartbeatSender(nil, common.ServerTypeAPI, "owned-api", "127.0.0.1", 9380)
	h.client = utility.NewHTTPClientBuilder().WithHost(host).WithPort(port).WithTimeout(time.Second).Build()
	for i := 0; i < 2; i++ {
		if h.SendHeartbeat() == nil || h.lastSuccess {
			t.Fatal("transport error reported success")
		}
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("transport failure prevented retry: %d", got)
	}
}
