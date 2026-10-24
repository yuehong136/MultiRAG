// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import "strings"

// NovitaModel routes both separate reasoning_content and inline think tags.
type NovitaModel struct{ *openAIChatDriver }

func NewNovitaModel(urls map[string]string, suffix URLSuffix) *NovitaModel {
	d := newOpenAIChatDriver(urls, suffix, "novita", novitaChatBody)
	d.inlineThink = true
	d.waitForStreamEOF = true
	return &NovitaModel{d}
}
func novitaChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config != nil && config.Thinking != nil {
		body["enable_thinking"] = *config.Thinking
	}
	return body, nil
}

func (n *NovitaModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(n.httpClient, n.BaseURL, n.URLSuffix.Embedding, name, texts, api, config)
}

func (n *NovitaModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(n.httpClient, n.BaseURL, n.URLSuffix.Rerank, name, query, texts, api, config, map[string]any{"return_documents": nil})
}

var _ ModelDriver = (*NovitaModel)(nil)

const (
	novitaThinkOpen  = "<think>"
	novitaThinkClose = "</think>"
)

// splitNovitaThink walks a complete content string and returns the
// visible portion + the concatenated chain-of-thought from inside
// any <think>...</think> blocks. Multiple think blocks are
// concatenated; tags themselves are stripped. Used by the
// non-streaming path where the whole content is available at once.
func splitNovitaThink(raw string) (visible, reasoning string) {
	var v, r strings.Builder
	inside := false
	for {
		var marker string
		if inside {
			marker = novitaThinkClose
		} else {
			marker = novitaThinkOpen
		}
		idx := strings.Index(raw, marker)
		if idx < 0 {
			if inside {
				r.WriteString(raw)
			} else {
				v.WriteString(raw)
			}
			break
		}
		if inside {
			r.WriteString(raw[:idx])
		} else {
			v.WriteString(raw[:idx])
		}
		raw = raw[idx+len(marker):]
		inside = !inside
	}
	return v.String(), r.String()
}

// novitaThinkExtractor maintains state across streaming chunks so
// that a <think>...</think> block spanning multiple SSE events still
// gets split correctly between content and reasoning. The buffer
// preserves only actual ASCII marker prefixes at the end of a chunk
// in case the next chunk completes a partial tag.
type novitaThinkExtractor struct {
	buf    strings.Builder
	inside bool
}

// novitaThinkSegment is one routing decision: emit `content` via the
// sender's first arg, or emit `reasoning` via the sender's second arg.
// Exactly one of the two fields is non-empty.
type novitaThinkSegment struct {
	content   string
	reasoning string
}

// Feed appends an incoming chunk and returns any segments that are
// now safe to emit. Trailing bytes that could be the start of a tag
// are held back in the buffer until the next call.
func (e *novitaThinkExtractor) Feed(chunk string) []novitaThinkSegment {
	e.buf.WriteString(chunk)
	s := e.buf.String()
	var out []novitaThinkSegment
	for {
		var marker, otherMarker string
		if e.inside {
			marker = novitaThinkClose
			otherMarker = novitaThinkOpen
		} else {
			marker = novitaThinkOpen
			otherMarker = novitaThinkClose
		}
		idx := strings.Index(s, marker)
		if idx < 0 {
			// Buffer only a suffix that really is a marker prefix. Every
			// marker is ASCII and begins with '<', so the emitted prefix ends
			// on a UTF-8 boundary for valid decoded SSE content. Arbitrarily
			// reserving seven bytes can split a Chinese/emoji rune and JSON
			// senders would replace its fragments with U+FFFD.
			reserve := 0
			for _, candidate := range []string{marker, otherMarker} {
				for size := 1; size < len(candidate) && size <= len(s); size++ {
					if strings.HasSuffix(s, candidate[:size]) {
						reserve = max(reserve, size)
					}
				}
			}
			safe := len(s) - reserve
			if safe > 0 {
				if e.inside {
					out = append(out, novitaThinkSegment{reasoning: s[:safe]})
				} else {
					out = append(out, novitaThinkSegment{content: s[:safe]})
				}
				s = s[safe:]
			}
			break
		}
		if idx > 0 {
			if e.inside {
				out = append(out, novitaThinkSegment{reasoning: s[:idx]})
			} else {
				out = append(out, novitaThinkSegment{content: s[:idx]})
			}
		}
		s = s[idx+len(marker):]
		e.inside = !e.inside
	}
	e.buf.Reset()
	e.buf.WriteString(s)
	return out
}

// Flush returns the buffered tail when the stream ends. A stream that
// ends mid-tag would not normally happen with a well-behaved upstream,
// but if it does the partial bytes are emitted according to the
// current mode so nothing is silently lost.
func (e *novitaThinkExtractor) Flush() *novitaThinkSegment {
	s := e.buf.String()
	e.buf.Reset()
	if s == "" {
		return nil
	}
	if e.inside {
		return &novitaThinkSegment{reasoning: s}
	}
	return &novitaThinkSegment{content: s}
}
