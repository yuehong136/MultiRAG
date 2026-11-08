//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

// stream.go defines the SSE event channel and the helper that formats
// events in the Python agent_api.py wire format. See plan §4.10.
//
// Phase 1 scope is the in-process channel and the SSE serializer. The
// HTTP writer wrapper (http.Flusher + chunked transfer) is deferred to
// Phase 5 when the canvas HTTP handler lands.
package canvas

import (
	"encoding/json"
	"errors"
	"sync"
)

// StreamEvent is the unit emitted by canvas components to the SSE writer.
// Field names match the Python "data" payload shape so a single
// frontend SSE parser can consume both runtimes.
type StreamEvent struct {
	// Event is the event name: "node_start" | "node_finish" | "message" | "error" | "cancelled" | ...
	Event string `json:"event"`
	// TaskID identifies the canvas run; required for client correlation.
	TaskID string `json:"task_id"`
	// Component identifies the canvas component that produced the event.
	Component string `json:"component,omitempty"`
	// Data is the free-form event body. SSE wire format is "data: " + json(ev.Data).
	Data map[string]any `json:"data,omitempty"`
}

// StreamEmitter pushes events toward an SSE writer. Emit must be
// non-blocking — a slow consumer must not stall canvas execution. The
// Phase-1 implementation drops events when the buffer is full and
// logs a warning; a Phase-5 SSE handler can swap in a back-pressured
// implementation if needed.
type StreamEmitter interface {
	Emit(ev StreamEvent) error
	Close() error
}

// channelEmitter is the default StreamEmitter: a buffered Go channel
// drained by an HTTP handler running in a separate goroutine.
type channelEmitter struct {
	mu     sync.Mutex
	closed bool
	ch     chan StreamEvent
}

// NewChannelEmitter returns a StreamEmitter backed by a buffered channel
// of the given size. Size 0 is valid (unbuffered) but will block Emit
// until a reader is ready — typically not what canvas runs want.
func NewChannelEmitter(buffer int) StreamEmitter {
	if buffer < 0 {
		buffer = 0
	}
	return &channelEmitter{ch: make(chan StreamEvent, buffer)}
}

// Emit pushes ev onto the channel. Non-blocking: if the buffer is full
// the event is dropped and a warning is logged. Returning a nil error
// on drop is intentional — the canvas run must keep going even if the
// SSE consumer is slow or absent.
var (
	ErrStreamClosed = errors.New("canvas: stream closed")
	ErrStreamFull   = errors.New("canvas: stream buffer full")
)

func (e *channelEmitter) Emit(ev StreamEvent) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return ErrStreamClosed
	}
	select {
	case e.ch <- ev:
		return nil
	default:
		return ErrStreamFull
	}
}

// Close is safe concurrently with Emit and may be repeated.
func (e *channelEmitter) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.closed {
		e.closed = true
		close(e.ch)
	}
	return nil
}

// Channel returns the underlying receive-only channel. It is exported
// (lowercase access from same package) only for tests; production code
// should consume via the StreamEmitter interface.
func (e *channelEmitter) Channel() <-chan StreamEvent {
	return e.ch
}

// FormatSSE renders ev into the Python agent_api.py wire format:
// `data: <json>\n\n`. JSON is emitted without HTML escaping so unicode
// stays readable. Errors marshaling Data fall back to a minimal
// `{"error": "..."}` payload so the SSE stream never gets a malformed
// frame.
func FormatSSE(ev StreamEvent) string {
	body, err := json.Marshal(ev.Data)
	if err != nil {
		body, _ = json.Marshal(map[string]string{
			"error":  "stream marshal failed",
			"detail": err.Error(),
		})
	}
	return "data: " + string(body) + "\n\n"
}
