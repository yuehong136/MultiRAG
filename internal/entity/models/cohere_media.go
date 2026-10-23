package models

func (c *CoHereModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(c.httpClient, c.BaseURL, c.URLSuffix.ASR, name, audio, api, config)
}

var _ ASRDriver = (*CoHereModel)(nil)
