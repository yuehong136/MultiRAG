package chunk

import (
	"fmt"
	"sort"
	"strings"
)

type overlapConfig struct {
	Size int    `json:"size"`
	Unit string `json:"unit,omitempty"`
}
type overlapCondition struct {
	Name       string        `json:"name"`
	If         string        `json:"if"`
	Then       overlapConfig `json:"then"`
	expression Expr
}
type postConfig struct {
	Merge *struct {
		TargetSize int    `json:"target_size"`
		Strategy   string `json:"strategy"`
	} `json:"merge"`
	Overlap *struct {
		Enabled    *bool              `json:"enabled"`
		Unit       string             `json:"unit"`
		Default    overlapConfig      `json:"default"`
		Conditions []overlapCondition `json:"conditions"`
	} `json:"overlap"`
	Filter *struct {
		MinLength int `json:"min_length"`
		MaxLength int `json:"max_length"`
	} `json:"filter"`
	Metadata *struct {
		IncludeIndex bool              `json:"include_index"`
		CustomFields map[string]string `json:"custom_fields"`
	} `json:"add_metadata"`
}

// PostprocessOperator merges, overlaps, filters and annotates split output.
// Configuration is immutable after compilation and safe to reuse concurrently.
type PostprocessOperator struct{ config postConfig }

func NewPostprocessOperator(config map[string]any) (*PostprocessOperator, error) {
	var c postConfig
	if err := decodeConfig(config, &c); err != nil {
		return nil, err
	}
	if c.Merge != nil {
		if c.Merge.TargetSize <= 0 {
			return nil, fmt.Errorf("merge target_size must be positive")
		}
		if c.Merge.Strategy == "" {
			c.Merge.Strategy = "greedy"
		}
		if c.Merge.Strategy != "greedy" {
			return nil, fmt.Errorf("unsupported merge strategy %q", c.Merge.Strategy)
		}
	}
	if c.Filter != nil && (c.Filter.MinLength < 0 || c.Filter.MaxLength < 0 || c.Filter.MaxLength > 0 && c.Filter.MinLength > c.Filter.MaxLength) {
		return nil, fmt.Errorf("invalid length filter")
	}
	if c.Overlap != nil {
		if c.Overlap.Unit == "" {
			c.Overlap.Unit = "char"
		}
		if err := validateOverlap(overlapConfig{Unit: c.Overlap.Unit}); err != nil {
			return nil, err
		}
		if err := validateOverlap(c.Overlap.Default); err != nil {
			return nil, err
		}
		for i := range c.Overlap.Conditions {
			condition := &c.Overlap.Conditions[i]
			if err := validateOverlap(condition.Then); err != nil {
				return nil, err
			}
			expression, err := CompileExpression(condition.If)
			if err != nil {
				return nil, fmt.Errorf("overlap condition %d: %w", i, err)
			}
			condition.expression = expression
		}
	}
	if c.Metadata != nil {
		for k, v := range c.Metadata.CustomFields {
			if v == "auto_detect" {
				switch k {
				case "has_media_url", "has_image_url", "has_video_url", "has_url", "language", "length":
				default:
					return nil, fmt.Errorf("unknown auto_detect field %q", k)
				}
			}
		}
	}
	return &PostprocessOperator{c}, nil
}
func validateOverlap(c overlapConfig) error {
	if c.Size < 0 {
		return fmt.Errorf("overlap size must be non-negative")
	}
	if c.Unit != "" && c.Unit != "char" && c.Unit != "sentence" {
		return fmt.Errorf("unsupported overlap unit %q", c.Unit)
	}
	return nil
}
func (o *PostprocessOperator) Prepare(*ChunkContext) error { return nil }
func (o *PostprocessOperator) Finish(*ChunkContext) error  { return nil }
func (o *PostprocessOperator) Execute(ctx *ChunkContext) error {
	chunks := make([]ChunkData, len(ctx.SplitChunks))
	for i, c := range ctx.SplitChunks {
		chunks[i] = c
		chunks[i].Metadata = copyMetadata(c.Metadata)
	}
	c := o.config
	if c.Merge != nil {
		chunks = mergeChunks(chunks, c.Merge.TargetSize)
	}
	if c.Overlap != nil && (c.Overlap.Enabled == nil || *c.Overlap.Enabled) {
		original := append([]ChunkData(nil), chunks...)
		for i := 1; i < len(chunks); i++ {
			cfg := c.Overlap.Default
			for _, condition := range c.Overlap.Conditions {
				matched, err := EvalCompiled(condition.expression, buildExprContext(&original[i], original[i].Metadata))
				if err != nil {
					return fmt.Errorf("overlap condition %q: %w", condition.Name, err)
				}
				if matched {
					cfg = condition.Then
					break
				}
			}
			if cfg.Unit == "" {
				cfg.Unit = c.Overlap.Unit
			}
			if cfg.Size == 0 {
				continue
			}
			previous := original[i-1].Content
			if cfg.Unit == "sentence" {
				sentences := splitSentencesGeneric(previous)
				start := max(0, len(sentences)-cfg.Size)
				previous = strings.Join(sentences[start:], " ")
			} else {
				runes := []rune(previous)
				previous = string(runes[max(0, len(runes)-cfg.Size):])
			}
			chunks[i].Content = previous + chunks[i].Content
		}
	}
	filtered := make([]ChunkData, 0, len(chunks))
	for _, item := range chunks {
		n := RuneCount(item.Content)
		if c.Filter != nil && (n < c.Filter.MinLength || c.Filter.MaxLength > 0 && n > c.Filter.MaxLength) {
			continue
		}
		item.Index = len(filtered)
		item.Size = n
		if c.Metadata != nil {
			if c.Metadata.IncludeIndex {
				item.Metadata["index"] = item.Index
			}
			for field, action := range c.Metadata.CustomFields {
				if action == "auto_detect" {
					switch field {
					case "language":
						item.Metadata[field] = DetectLanguage(item.Content)
					default:
						item.Metadata[field] = buildExprContext(&item, nil)[field]
					}
				} else {
					item.Metadata[field] = action
				}
			}
		}
		filtered = append(filtered, item)
	}
	ctx.ResultChunks = filtered
	return nil
}
func copyMetadata(src map[string]any) map[string]any {
	dst := make(map[string]any, len(src))
	for k, v := range src {
		dst[k] = v
	}
	return dst
}
func mergeChunks(chunks []ChunkData, target int) []ChunkData {
	out := make([]ChunkData, 0, len(chunks))
	for _, c := range chunks {
		if len(out) == 0 || RuneCount(out[len(out)-1].Content)+1+RuneCount(c.Content) > target {
			out = append(out, c)
			continue
		}
		last := &out[len(out)-1]
		last.Content += " " + c.Content
		for k, v := range c.Metadata {
			last.Metadata[k] = v
		}
	}
	return out
}
func (o *PostprocessOperator) String() string {
	var b strings.Builder
	c := o.config
	b.WriteString("postprocess:\n")
	if c.Merge != nil {
		fmt.Fprintf(&b, "  merge: target_size=%d strategy=%s\n", c.Merge.TargetSize, c.Merge.Strategy)
	}
	if c.Overlap != nil {
		enabled := c.Overlap.Enabled == nil || *c.Overlap.Enabled
		fmt.Fprintf(&b, "  overlap: enabled=%t unit=%s default=%d\n", enabled, c.Overlap.Unit, c.Overlap.Default.Size)
		for _, condition := range c.Overlap.Conditions {
			fmt.Fprintf(&b, "    %s: %s -> size=%d unit=%s\n", condition.Name, condition.expression.String(), condition.Then.Size, condition.Then.Unit)
		}
	}
	if c.Filter != nil {
		fmt.Fprintf(&b, "  filter: min_length=%d max_length=%d\n", c.Filter.MinLength, c.Filter.MaxLength)
	}
	if c.Metadata != nil {
		fmt.Fprintf(&b, "  add_metadata: include_index=%t\n", c.Metadata.IncludeIndex)
		keys := make([]string, 0, len(c.Metadata.CustomFields))
		for k := range c.Metadata.CustomFields {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			fmt.Fprintf(&b, "    %s: %s\n", k, c.Metadata.CustomFields[k])
		}
	}
	return b.String()
}
func splitSentencesGeneric(text string) []string {
	var sentences []string
	var b strings.Builder
	for _, r := range text {
		b.WriteRune(r)
		if strings.ContainsRune("。！？.!?", r) {
			sentences = append(sentences, strings.TrimSpace(b.String()))
			b.Reset()
		}
	}
	if b.Len() > 0 {
		sentences = append(sentences, strings.TrimSpace(b.String()))
	}
	return sentences
}
