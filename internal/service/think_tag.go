package service

import "strings"

// ThinkStreamState consumes provider deltas (never cumulative snapshots). Only
// a possible tag suffix is buffered, so splitting a tag cannot expose thought.
// Nested and unclosed blocks remain in the independent reasoning channel.
type ThinkStreamState struct {
	pending string
	depth   int
}

func (s *ThinkStreamState) Feed(delta string) (visible, reasoning string) {
	s.pending += delta
	var answer, thought strings.Builder
	emit := func(text string) {
		if s.depth > 0 {
			thought.WriteString(text)
		} else {
			answer.WriteString(text)
		}
	}
	for s.pending != "" {
		lower := strings.ToLower(s.pending)
		if strings.HasPrefix(lower, "<think>") {
			s.depth++
			s.pending = s.pending[len("<think>"):]
			continue
		}
		if strings.HasPrefix(lower, "</think>") {
			if s.depth > 0 {
				s.depth--
			}
			s.pending = s.pending[len("</think>"):]
			continue
		}
		if strings.HasPrefix("<think>", lower) || strings.HasPrefix("</think>", lower) {
			break
		}
		next := strings.Index(s.pending[1:], "<")
		n := len(s.pending)
		if next >= 0 {
			n = next + 1
		}
		emit(s.pending[:n])
		s.pending = s.pending[n:]
	}
	return answer.String(), thought.String()
}

// Finish discards ambiguous tag prefixes and unclosed private content at EOF.
func (s *ThinkStreamState) Finish() { s.pending = "" }

func ExtractVisibleAnswer(raw string) string {
	var s ThinkStreamState
	visible, _ := s.Feed(raw)
	s.Finish()
	return visible
}
