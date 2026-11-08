package deepdoc

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"mime/multipart"
)

type BBox [4]float64
type DLAResult struct {
	Type    string  `json:"type"`
	Score   float64 `json:"score"`
	BBox    BBox    `json:"bbox"`
	TypeIdx int     `json:"type_idx"`
}

var dlaClasses = [...]string{"title", "text", "reference", "figure", "figure caption", "table", "table caption", "table caption", "equation", "figure caption"}

type rawDLA struct {
	BBoxes [][]float64 `json:"bboxes"`
}

// DLA preserves image grouping. Failure is an error, never a fake empty region.
// An empty detections slice is valid only when the server returns bboxes:[].
func (c *Client) DLA(ctx context.Context, images [][]byte) ([][]DLAResult, error) {
	if c == nil || c.baseURL == "" {
		return nil, ErrNoURL
	}
	if c.configErr != nil {
		return nil, c.configErr
	}
	if len(images) > 32 {
		return nil, fmt.Errorf("deepdoc: at most 32 images per call")
	}
	total := 0
	for _, image := range images {
		if len(image) == 0 || len(image) > 8<<20 {
			return nil, fmt.Errorf("deepdoc: image size must be 1..8MiB")
		}
		total += len(image)
	}
	if total > 32<<20 {
		return nil, fmt.Errorf("deepdoc: batch size exceeds 32MiB")
	}
	out := make([][]DLAResult, 0, len(images))
	for i, image := range images {
		result, err := c.predictOne(ctx, image)
		if err != nil {
			return nil, fmt.Errorf("deepdoc image %d: %w", i, err)
		}
		out = append(out, result)
	}
	return out, nil
}
func (c *Client) predictOne(ctx context.Context, image []byte) ([]DLAResult, error) {
	build := func() (io.Reader, string) {
		var b bytes.Buffer
		writer := multipart.NewWriter(&b)
		// bytes.Buffer cannot fail writes; headers are constant.
		part, _ := writer.CreatePart(map[string][]string{
			"Content-Disposition": {`form-data; name="request"; filename="image.jpg"`},
			"Content-Type":        {"image/jpeg"},
		})
		part.Write(image)
		writer.Close()
		return &b, writer.FormDataContentType()
	}
	validate := func(data []byte) error {
		var raw rawDLA
		if err := json.Unmarshal(data, &raw); err != nil {
			return fmt.Errorf("%w: malformed JSON", ErrInvalidResponse)
		}
		if raw.BBoxes == nil {
			return fmt.Errorf("%w: missing bboxes", ErrInvalidResponse)
		}
		for _, box := range raw.BBoxes {
			if len(box) != 6 {
				return fmt.Errorf("%w: bbox requires 6 values", ErrInvalidResponse)
			}
			for _, value := range box {
				if math.IsNaN(value) || math.IsInf(value, 0) {
					return ErrInvalidResponse
				}
			}
			if box[0] < 0 || box[1] < 0 || box[2] < box[0] || box[3] < box[1] || box[4] < 0 || box[4] > 1 || box[5] != math.Trunc(box[5]) || box[5] < 0 || box[5] >= float64(len(dlaClasses)) {
				return fmt.Errorf("%w: invalid bbox values", ErrInvalidResponse)
			}
		}
		return nil
	}
	data, err := c.doPost(ctx, build, validate)
	if err != nil {
		return nil, err
	}
	var raw rawDLA
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, err
	}
	results := make([]DLAResult, 0, len(raw.BBoxes))
	for _, box := range raw.BBoxes {
		index := int(box[5])
		results = append(results, DLAResult{dlaClasses[index], box[4], BBox{box[0], box[1], box[2], box[3]}, index})
	}
	return results, nil
}
