package handler

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity/models"
	"net/http"
)

type mediaIdentity struct {
	ProviderName string `json:"provider_name" binding:"required"`
	InstanceName string `json:"instance_name" binding:"required"`
	ModelName    string `json:"model_name" binding:"required"`
}
type TranscribeAudioRequest struct {
	mediaIdentity
	File     string   `json:"file" binding:"required"`
	Language []string `json:"language,omitempty"`
	Prompt   string   `json:"prompt,omitempty"`
	Format   string   `json:"format,omitempty"`
	Stream   bool     `json:"stream"`
}
type AudioSpeechRequest struct {
	mediaIdentity
	Text   string `json:"text" binding:"required"`
	Voice  string `json:"voice,omitempty"`
	Format string `json:"format,omitempty"`
	Stream bool   `json:"stream"`
}
type OCRFileRequest struct {
	mediaIdentity
	Content  string   `json:"content" binding:"required"`
	Language []string `json:"language,omitempty"`
}

func bindMedia(c *gin.Context, req any) (string, bool) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return "", false
	}
	// JSON byte fields are base64; enforce the encoded bound before binding/allocating.
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, int64(base64.StdEncoding.EncodedLen(models.MaxMediaBytes)+64*1024))
	if err := c.ShouldBindJSON(req); err != nil {
		jsonError(c, common.CodeBadRequest, "invalid media request")
		return "", false
	}
	return user.ID, true
}
func writeMediaEvent(c *gin.Context, event string, data any) error {
	if err := c.Request.Context().Err(); err != nil {
		return err
	}
	var payload []byte
	var err error
	if marker, ok := data.(string); ok && marker == "[DONE]" {
		payload = []byte(marker)
	} else {
		payload, err = json.Marshal(data)
	}
	if err != nil {
		return err
	}
	if _, err = fmt.Fprintf(c.Writer, "event: %s\ndata: %s\n\n", event, payload); err != nil {
		return err
	}
	c.Writer.Flush()
	return nil
}
func mediaStream(c *gin.Context, run func(func(any) error) (common.ErrorCode, error)) {
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Writer.WriteHeader(http.StatusOK)
	code, err := run(func(data any) error { return writeMediaEvent(c, "message", data) })
	if err != nil || code != common.CodeSuccess {
		message := "media inference failed"
		if err != nil {
			message = err.Error()
		}
		if code == common.CodeSuccess {
			code = common.CodeServerError
		}
		_ = writeMediaEvent(c, "error", gin.H{"code": code, "message": message})
		return
	}
	_ = writeMediaEvent(c, "done", "[DONE]")
}
func (h *ProviderHandler) TranscribeAudio(c *gin.Context) {
	var req TranscribeAudioRequest
	user, ok := bindMedia(c, &req)
	if !ok {
		return
	}
	audio, err := models.DecodeMediaData(req.File)
	if err != nil {
		jsonError(c, common.CodeBadRequest, err.Error())
		return
	}
	api := &models.APIConfig{Context: c.Request.Context()}
	config := &models.ASRConfig{Language: req.Language, Prompt: req.Prompt, Format: req.Format}
	if req.Stream {
		mediaStream(c, func(send func(any) error) (common.ErrorCode, error) {
			return h.modelProviderService.TranscribeAudioStream(req.ProviderName, req.InstanceName, req.ModelName, user, audio, api, config, func(r *models.ASRResponse) error { return send(r) })
		})
		return
	}
	data, code, err := h.modelProviderService.TranscribeAudio(req.ProviderName, req.InstanceName, req.ModelName, user, audio, api, config)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, data, "success")
}
func (h *ProviderHandler) AudioSpeech(c *gin.Context) {
	var req AudioSpeechRequest
	user, ok := bindMedia(c, &req)
	if !ok {
		return
	}
	api := &models.APIConfig{Context: c.Request.Context()}
	config := &models.TTSConfig{Voice: req.Voice, Format: req.Format}
	if req.Stream {
		mediaStream(c, func(send func(any) error) (common.ErrorCode, error) {
			return h.modelProviderService.AudioSpeechStream(req.ProviderName, req.InstanceName, req.ModelName, user, req.Text, api, config, func(r *models.TTSResponse) error { return send(r) })
		})
		return
	}
	data, code, err := h.modelProviderService.AudioSpeech(req.ProviderName, req.InstanceName, req.ModelName, user, req.Text, api, config)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, data, "success")
}
func (h *ProviderHandler) OCRFile(c *gin.Context) {
	var req OCRFileRequest
	user, ok := bindMedia(c, &req)
	if !ok {
		return
	}
	content, err := models.DecodeMediaData(req.Content)
	if err != nil {
		jsonError(c, common.CodeBadRequest, err.Error())
		return
	}
	data, code, err := h.modelProviderService.OCRFile(req.ProviderName, req.InstanceName, req.ModelName, user, content, &models.APIConfig{Context: c.Request.Context()}, &models.OCRConfig{Language: req.Language})
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, data, "success")
}
