package models

import (
	"context"
	"time"
)

const (
	nonStreamCallTimeout = 2 * time.Minute
	streamCallTimeout    = 10 * time.Minute
	longOpCallTimeout    = 10 * time.Minute
)

// modelOperationConfig copies request settings and preserves parent cancellation.
// The operation owns cancel until all response bodies and sender calls complete.
func modelOperationConfig(api *APIConfig, timeout time.Duration) (*APIConfig, context.CancelFunc) {
	local := APIConfig{}
	if api != nil {
		local = *api
	}
	ctx, cancel := context.WithTimeout(requestContext(api), timeout)
	local.Context = ctx
	return &local, cancel
}

func modelCallConfig(api *APIConfig) (*APIConfig, context.CancelFunc) {
	return modelOperationConfig(api, nonStreamCallTimeout)
}
func modelStreamConfig(api *APIConfig) (*APIConfig, context.CancelFunc) {
	return modelOperationConfig(api, streamCallTimeout)
}
func modelLongOpConfig(api *APIConfig) (*APIConfig, context.CancelFunc) {
	return modelOperationConfig(api, longOpCallTimeout)
}
