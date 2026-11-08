//
// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

package chunk

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

type SplitOperator struct {
	strategy       string
	boundaries     []string
	keepSeparators bool
}

func NewSplitOperator(config map[string]interface{}) (*SplitOperator, error) {
	var cfg struct {
		Strategy string `json:"strategy"`
		Params   struct {
			Boundaries     []string `json:"boundaries"`
			KeepSeparators bool     `json:"keep_separators"`
		} `json:"params"`
	}
	if err := decodeConfig(config, &cfg); err != nil {
		return nil, err
	}
	if cfg.Strategy == "" {
		cfg.Strategy = "sentence"
	}
	switch cfg.Strategy {
	case "sentence", "char", "paragraph":
	default:
		return nil, fmt.Errorf("unsupported split strategy %q", cfg.Strategy)
	}
	if len(cfg.Params.Boundaries) == 0 {
		cfg.Params.Boundaries = []string{"。", "！", "？", "\n"}
	}
	for _, b := range cfg.Params.Boundaries {
		if b == "" {
			return nil, fmt.Errorf("empty split boundary")
		}
	}
	return &SplitOperator{cfg.Strategy, cfg.Params.Boundaries, cfg.Params.KeepSeparators}, nil
}

func (o *SplitOperator) Prepare(ctx *ChunkContext) error {
	return nil
}

func (o *SplitOperator) Execute(ctx *ChunkContext) error {
	text := ctx.TextAfterPreprocess

	switch o.strategy {
	case "sentence":
		ctx.SplitChunks = o.splitSentences(text)
	case "char":
		ctx.SplitChunks = o.splitByChar(text)
	case "paragraph":
		ctx.SplitChunks = o.splitByParagraph(text)
	default:
		ctx.SplitChunks = o.splitSentences(text)
	}

	return nil
}

func (o *SplitOperator) Finish(ctx *ChunkContext) error {
	return nil
}

func (o *SplitOperator) String() string {
	var buf strings.Builder
	buf.WriteString("split:\n")
	fmt.Fprintf(&buf, "  strategy: %q\n", o.strategy)
	fmt.Fprintf(&buf, "  boundaries:\n")
	for _, r := range o.boundaries {
		fmt.Fprintf(&buf, "    - %q\n", r)
	}
	fmt.Fprintf(&buf, "  keep_separators: %t\n", o.keepSeparators)
	return buf.String()
}

// splitSentences splits text at multi-rune boundaries, optionally keeping separators.
func (o *SplitOperator) splitSentences(text string) []ChunkData {
	var chunks []ChunkData
	var buf strings.Builder
	i := 0

	for i < len(text) {
		// Try to match any boundary at current position (first match wins)
		matchedBound := ""
		for _, bound := range o.boundaries {
			if bound != "" && i+len(bound) <= len(text) && text[i:i+len(bound)] == bound {
				matchedBound = bound
				break
			}
		}

		if matchedBound != "" {
			if o.keepSeparators {
				buf.WriteString(matchedBound)
			}
			if buf.Len() > 0 {
				chunks = append(chunks, ChunkData{
					Content: buf.String(),
					Index:   len(chunks),
					Metadata: map[string]interface{}{
						"language": DetectLanguage(buf.String()),
					},
				})
				buf.Reset()
			}
			i += len(matchedBound)
		} else {
			r, size := utf8.DecodeRuneInString(text[i:])
			buf.WriteRune(r)
			i += size
		}
	}

	// flush remaining text
	if buf.Len() > 0 {
		chunks = append(chunks, ChunkData{
			Content: buf.String(),
			Index:   len(chunks),
			Metadata: map[string]interface{}{
				"language": DetectLanguage(buf.String()),
			},
		})
	}

	return chunks
}

func (o *SplitOperator) splitByChar(text string) []ChunkData {
	var chunks []ChunkData
	for len(text) > 0 {
		r, size := utf8.DecodeRuneInString(text)
		chunks = append(chunks, ChunkData{
			Content:  string(r),
			Index:    len(chunks),
			Metadata: map[string]interface{}{"language": DetectLanguage(string(r))},
		})
		text = text[size:]
	}
	return chunks
}

func (o *SplitOperator) splitByParagraph(text string) []ChunkData {
	paragraphs := strings.Split(text, "\n")
	chunks := make([]ChunkData, 0, len(paragraphs))
	for _, p := range paragraphs {
		trimmed := strings.TrimSpace(p)
		if trimmed == "" {
			continue
		}
		chunks = append(chunks, ChunkData{
			Content:  trimmed,
			Index:    len(chunks),
			Metadata: map[string]interface{}{"language": DetectLanguage(trimmed)},
		})
	}
	return chunks
}
