package service

import (
	"context"
	"fmt"
	"math"
	"regexp"
	"strings"
	"unicode/utf8"

	"multirag/internal/entity/models"
)

var answerCitationRE = regexp.MustCompile(`\[ID:([^\]\r\n]+)\]`)

type citationSpan struct{ start, end int }

// citationSpans preserves every original byte and excludes fenced code. Both
// closed and unfinished fences stay untouched; no reconstruction normalizes them.
func citationSpans(answer string) []citationSpan {
	spans := []citationSpan{}
	start, inCode := 0, false
	add := func(end int) {
		if utf8.RuneCountInString(strings.TrimSpace(answer[start:end])) >= 5 {
			spans = append(spans, citationSpan{start, end})
		}
		start = end
	}
	for i := 0; i < len(answer); {
		if strings.HasPrefix(answer[i:], "```") {
			if !inCode {
				add(i)
			}
			inCode = !inCode
			i += 3
			start = i
			continue
		}
		r, n := utf8.DecodeRuneInString(answer[i:])
		i += n
		if inCode {
			start = i
			continue
		}
		if strings.ContainsRune("。！？；.?!;\n", r) {
			add(i)
		}
	}
	if !inCode {
		add(len(answer))
	}
	return spans
}

// cleanAnswerCitations retains only IDs actually present in the prompt. Pending
// prefixes are held back from cumulative stream snapshots until complete.
func cleanAnswerCitations(answer string, chunks []SourcedChunk, enabled bool) (string, []SourcedChunk) {
	allowed := map[string]bool{}
	used := map[string]bool{}
	for _, chunk := range chunks {
		allowed[chunk.ID] = true
	}
	answer = answerCitationRE.ReplaceAllStringFunc(answer, func(marker string) string {
		id := answerCitationRE.FindStringSubmatch(marker)[1]
		if enabled && allowed[id] {
			used[id] = true
			return marker
		}
		return ""
	})
	if pos := strings.LastIndex(answer, "["); pos >= 0 {
		tail := answer[pos:]
		if strings.HasPrefix("[ID:", tail) || strings.HasPrefix(tail, "[ID:") && !strings.Contains(tail, "]") {
			answer = answer[:pos]
		}
	}
	referenced := []SourcedChunk{}
	for _, chunk := range chunks {
		if used[chunk.ID] {
			referenced = append(referenced, chunk)
		}
	}
	return answer, referenced
}

// finalizeCitations uses the exact owner-bound embedder from retrieval and
// scoped vector hydration. Missing/zero stored vectors never create evidence.
func (s *ChunkService) finalizeCitations(ctx context.Context, actor, answer string, chunks []SourcedChunk, result *RetrievalTestResponse, enabled bool) (string, map[string]interface{}, error) {
	answer, referenced := cleanAnswerCitations(answer, chunks, enabled)
	if strings.TrimSpace(answer) == "" || !enabled || len(chunks) == 0 {
		return answer, answerReference(nil), ctx.Err()
	}
	if len(referenced) > 0 {
		return answer, answerReference(referenced), ctx.Err()
	}
	spans := citationSpans(answer)
	if len(spans) == 0 {
		return answer, answerReference(nil), ctx.Err()
	}
	if result == nil || result.embedding == nil {
		return "", nil, fmt.Errorf("citation embedding unavailable")
	}
	texts := make([]string, len(spans))
	for i, span := range spans {
		texts[i] = answer[span.start:span.end]
	}
	data, err := result.embedding.Embed(texts)
	if err != nil {
		return "", nil, err
	}
	sentenceVectors, err := models.EmbeddingVectors(data, len(texts))
	if err != nil {
		return "", nil, err
	}
	dim := len(sentenceVectors[0])
	vectors := make([][]float64, len(chunks))
	groups := map[string][]int{}
	for i, chunk := range chunks {
		if chunk.DatasetID != "" && chunk.DocID != "" {
			groups[chunk.DatasetID] = append(groups[chunk.DatasetID], i)
		}
	}
	// Iterate prompt order for deterministic requests and stable failure evidence.
	seen := map[string]bool{}
	for _, chunk := range chunks {
		kb := chunk.DatasetID
		if seen[kb] || len(groups[kb]) == 0 {
			continue
		}
		seen[kb] = true
		ids, docs := []string{}, []string{}
		for _, i := range groups[kb] {
			ids = append(ids, chunks[i].ID)
			docs = append(docs, chunks[i].DocID)
		}
		hydrated, err := s.FetchChunkVectors(ctx, &ChunkVectorRequest{DatasetID: kb, DocumentIDs: docs, ChunkIDs: ids, Dimension: dim}, actor)
		if err != nil {
			return "", nil, err
		}
		for _, i := range groups[kb] {
			vectors[i] = hydrated.Vectors[chunks[i].ID]
		}
	}
	decorated, used, err := insertCitationsWithVectors(answer, spans, chunks, sentenceVectors, vectors)
	if err != nil {
		return "", nil, err
	}
	if err := ctx.Err(); err != nil {
		return "", nil, err
	}
	referenced = []SourcedChunk{}
	for i, chunk := range chunks {
		if used[i] {
			referenced = append(referenced, chunk)
		}
	}
	return decorated, answerReference(referenced), nil
}

func insertCitationsWithVectors(answer string, spans []citationSpan, chunks []SourcedChunk, sentences, vectors [][]float64) (string, map[int]bool, error) {
	if len(spans) != len(sentences) || len(chunks) != len(vectors) {
		return "", nil, fmt.Errorf("citation vector count mismatch")
	}
	if err := models.ValidateEmbeddings(sentences, len(spans)); err != nil {
		return "", nil, err
	}
	used := map[int]bool{}
	matches := map[int][]int{}
	dim := 0
	if len(sentences) > 0 {
		dim = len(sentences[0])
	}
	for _, v := range vectors {
		if v == nil {
			continue
		}
		if _, err := parseChunkVector(v, dim); err != nil {
			return "", nil, err
		}
	}
	for threshold := 0.63; threshold > 0.3 && len(matches) == 0; threshold *= 0.8 {
		for i, sv := range sentences {
			scores := make([]float64, len(vectors))
			best := 0.0
			for j, cv := range vectors {
				if cv == nil {
					continue
				}
				scores[j] = citationCosine(sv, cv)
				best = math.Max(best, scores[j])
			}
			if best*0.99 < threshold {
				continue
			}
			for j, score := range scores {
				if score > best*0.99 && len(matches[i]) < 4 {
					matches[i] = append(matches[i], j)
				}
			}
		}
	}
	var out strings.Builder
	position := 0
	for i, span := range spans {
		if span.start < position || span.end < span.start || span.end > len(answer) {
			return "", nil, fmt.Errorf("invalid citation span")
		}
		out.WriteString(answer[position:span.end])
		position = span.end
		for _, j := range matches[i] {
			out.WriteString(" [ID:" + chunks[j].ID + "]")
			used[j] = true
		}
	}
	out.WriteString(answer[position:])
	return out.String(), used, nil
}

func citationCosine(a, b []float64) float64 {
	na, nb := 0.0, 0.0
	for i := range a {
		na = math.Hypot(na, a[i])
		nb = math.Hypot(nb, b[i])
	}
	if na == 0 || nb == 0 {
		return 0
	}
	score := 0.0
	for i := range a {
		score += (a[i] / na) * (b[i] / nb)
	}
	return score
}
