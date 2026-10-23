package service

import (
	"context"
	"errors"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"slices"
	"time"
)

func mediaContext(api *models.APIConfig) context.Context {
	if api != nil && api.Context != nil {
		return api.Context
	}
	return context.Background()
}
func mediaError(err error) (common.ErrorCode, error) {
	if errors.Is(err, models.ErrUnsupportedCapability) {
		return common.CodeNotImplemented, err
	}
	return common.CodeServerError, err
}

// mediaModel gives the driver only SQL-selected credentials and a bounded call context.
func (m *ModelProviderService) mediaModel(provider, instance, name, user string, typ entity.ModelType, api *models.APIConfig) (models.ModelDriver, *models.APIConfig, context.CancelFunc, common.ErrorCode, error) {
	ctx, cancel := context.WithTimeout(mediaContext(api), 120*time.Second)
	if err := ctx.Err(); err != nil {
		cancel()
		return nil, nil, nil, common.CodeServerError, err
	}
	driver, resolved, code, err := m.userInferenceModel(provider, instance, name, user, typ, &models.APIConfig{Context: ctx})
	if err != nil {
		cancel()
		return nil, nil, nil, code, err
	}
	return driver, resolved, cancel, common.CodeSuccess, nil
}

func copyASRConfig(config *models.ASRConfig) *models.ASRConfig {
	result := models.ASRConfig{}
	if config != nil {
		result = *config
		result.Language = slices.Clone(config.Language)
	}
	return &result
}
func (m *ModelProviderService) TranscribeAudio(provider, instance, name, user string, audio []byte, api *models.APIConfig, config *models.ASRConfig) (*models.ASRResponse, common.ErrorCode, error) {
	if err := models.ValidateMediaBytes(audio); err != nil {
		return nil, common.CodeBadRequest, err
	}
	if err := models.ValidateASRConfig(config); err != nil {
		return nil, common.CodeBadRequest, err
	}
	driver, resolved, cancel, code, err := m.mediaModel(provider, instance, name, user, entity.ModelTypeSpeech2Text, api)
	if err != nil {
		return nil, code, err
	}
	defer cancel()
	capability, ok := driver.(models.ASRDriver)
	if !ok {
		return nil, common.CodeNotImplemented, fmt.Errorf("%w: ASR", models.ErrUnsupportedCapability)
	}
	result, err := capability.TranscribeAudio(&name, audio, resolved, copyASRConfig(config))
	if err == nil {
		err = mediaContext(resolved).Err()
	}
	if err == nil {
		err = models.ValidateASRResponse(result)
	}
	if err != nil {
		code, err = mediaError(err)
		return nil, code, err
	}
	return result, common.CodeSuccess, nil
}
func (m *ModelProviderService) AudioSpeech(provider, instance, name, user, text string, api *models.APIConfig, config *models.TTSConfig) (*models.TTSResponse, common.ErrorCode, error) {
	if err := models.ValidateSpeechText(text); err != nil {
		return nil, common.CodeBadRequest, err
	}
	if err := models.ValidateTTSConfig(config); err != nil {
		return nil, common.CodeBadRequest, err
	}
	driver, resolved, cancel, code, err := m.mediaModel(provider, instance, name, user, entity.ModelTypeTTS, api)
	if err != nil {
		return nil, code, err
	}
	defer cancel()
	capability, ok := driver.(models.TTSDriver)
	if !ok {
		return nil, common.CodeNotImplemented, fmt.Errorf("%w: TTS", models.ErrUnsupportedCapability)
	}
	cfg := models.TTSConfig{}
	if config != nil {
		cfg = *config
	}
	wantedFormat := cfg.Format
	result, err := capability.AudioSpeech(&name, text, resolved, &cfg)
	if err == nil {
		err = mediaContext(resolved).Err()
	}
	if err == nil {
		err = models.ValidateTTSResponse(result)
		if err == nil && wantedFormat != "" && result.Format != wantedFormat {
			err = fmt.Errorf("speech format does not match request")
		}
	}
	if err != nil {
		code, err = mediaError(err)
		return nil, code, err
	}
	return result, common.CodeSuccess, nil
}
func (m *ModelProviderService) OCRFile(provider, instance, name, user string, content []byte, api *models.APIConfig, config *models.OCRConfig) (*models.OCRResponse, common.ErrorCode, error) {
	if err := models.ValidateMediaBytes(content); err != nil {
		return nil, common.CodeBadRequest, err
	}
	return m.ocrInput(provider, instance, name, user, content, "", api, config)
}
func (m *ModelProviderService) OCRURL(provider, instance, name, user, imageURL string, api *models.APIConfig, config *models.OCRConfig) (*models.OCRResponse, common.ErrorCode, error) {
	if err := models.ValidateOCRURL(imageURL); err != nil {
		return nil, common.CodeBadRequest, err
	}
	return m.ocrInput(provider, instance, name, user, nil, imageURL, api, config)
}
func (m *ModelProviderService) ocrInput(provider, instance, name, user string, content []byte, imageURL string, api *models.APIConfig, config *models.OCRConfig) (*models.OCRResponse, common.ErrorCode, error) {
	driver, resolved, cancel, code, err := m.mediaModel(provider, instance, name, user, entity.ModelTypeOCR, api)
	if err != nil {
		return nil, code, err
	}
	defer cancel()

	cfg := models.OCRConfig{}
	if config != nil {
		cfg = *config
		cfg.Language = slices.Clone(config.Language)
	}
	var result *models.OCRResponse
	if imageURL != "" {
		capability, ok := driver.(models.OCRURLDriver)
		if !ok {
			return nil, common.CodeNotImplemented, fmt.Errorf("%w: OCR URL", models.ErrUnsupportedCapability)
		}
		result, err = capability.OCRURL(&name, imageURL, resolved, &cfg)
	} else {
		capability, ok := driver.(models.OCRDriver)
		if !ok {
			return nil, common.CodeNotImplemented, fmt.Errorf("%w: OCR", models.ErrUnsupportedCapability)
		}
		result, err = capability.OCRFile(&name, content, resolved, &cfg)
	}
	if err == nil {
		err = mediaContext(resolved).Err()
	}
	if err == nil {
		err = models.ValidateOCRResponse(result)
	}
	if err != nil {
		code, err = mediaError(err)
		return nil, code, err
	}
	return result, common.CodeSuccess, nil
}
func (m *ModelProviderService) TranscribeAudioStream(provider, instance, name, user string, audio []byte, api *models.APIConfig, config *models.ASRConfig, sender func(*models.ASRResponse) error) (common.ErrorCode, error) {
	if err := models.ValidateMediaBytes(audio); err != nil {
		return common.CodeBadRequest, err
	}
	if sender == nil {
		return common.CodeBadRequest, fmt.Errorf("sender required")
	}
	if err := models.ValidateASRConfig(config); err != nil {
		return common.CodeBadRequest, err
	}
	driver, resolved, cancel, code, err := m.mediaModel(provider, instance, name, user, entity.ModelTypeSpeech2Text, api)
	if err != nil {
		return code, err
	}
	defer cancel()
	capability, ok := driver.(models.ASRStreamDriver)
	if !ok {
		return common.CodeNotImplemented, fmt.Errorf("%w: streaming ASR", models.ErrUnsupportedCapability)
	}
	size, events := 0, 0
	var sendErr error
	err = capability.TranscribeAudioWithSender(&name, audio, resolved, copyASRConfig(config), func(result *models.ASRResponse) error {
		if sendErr != nil {
			return sendErr
		}
		sendErr = mediaContext(resolved).Err()
		if sendErr == nil {
			sendErr = models.ValidateASRResponse(result)
		}
		if sendErr == nil {
			events++
			size += len(*result.Text)
			if size > models.MaxMediaBytes {
				sendErr = fmt.Errorf("transcription stream exceeds 20 MiB")
			} else {
				sendErr = sender(result)
			}
		}
		return sendErr
	})
	if sendErr != nil {
		err = sendErr
	}
	if err == nil {
		err = mediaContext(resolved).Err()
	}
	if err == nil && events == 0 {
		err = fmt.Errorf("empty transcription stream")
	}
	if err != nil {
		return mediaError(err)
	}
	return common.CodeSuccess, nil
}
func (m *ModelProviderService) AudioSpeechStream(provider, instance, name, user, text string, api *models.APIConfig, config *models.TTSConfig, sender func(*models.TTSResponse) error) (common.ErrorCode, error) {
	if err := models.ValidateSpeechText(text); err != nil {
		return common.CodeBadRequest, err
	}
	if sender == nil {
		return common.CodeBadRequest, fmt.Errorf("sender required")
	}
	if err := models.ValidateTTSConfig(config); err != nil {
		return common.CodeBadRequest, err
	}
	driver, resolved, cancel, code, err := m.mediaModel(provider, instance, name, user, entity.ModelTypeTTS, api)
	if err != nil {
		return code, err
	}
	defer cancel()
	capability, ok := driver.(models.TTSStreamDriver)
	if !ok {
		return common.CodeNotImplemented, fmt.Errorf("%w: streaming TTS", models.ErrUnsupportedCapability)
	}
	cfg := models.TTSConfig{}
	if config != nil {
		cfg = *config
	}
	size, format := 0, cfg.Format
	var sendErr error
	err = capability.AudioSpeechWithSender(&name, text, resolved, &cfg, func(result *models.TTSResponse) error {
		if sendErr != nil {
			return sendErr
		}
		sendErr = mediaContext(resolved).Err()
		if sendErr == nil {
			sendErr = models.ValidateTTSResponse(result)
		}
		if sendErr == nil {
			if format != "" && format != result.Format {
				sendErr = fmt.Errorf("speech format changed during stream")
			} else {
				format = result.Format
				size += len(result.Audio)
				if size > models.MaxMediaBytes {
					sendErr = fmt.Errorf("speech stream exceeds 20 MiB")
				} else {
					sendErr = sender(result)
				}
			}
		}
		return sendErr
	})
	if sendErr != nil {
		err = sendErr
	}
	if err == nil {
		err = mediaContext(resolved).Err()
	}
	if err == nil && size == 0 {
		err = fmt.Errorf("empty speech stream")
	}
	if err != nil {
		return mediaError(err)
	}
	return common.CodeSuccess, nil
}
