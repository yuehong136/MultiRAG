package service

import (
	"context"
	"errors"
	"multirag/internal/common"
	"testing"
)

func TestProviderBalanceCancelledBeforeStorage(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	result, code, err := (&ModelProviderService{}).ShowInstanceBalance(ctx, "Gitee", "owned", "owner")
	if result != nil || code != common.CodeServerError || !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled balance touched storage or lost cancellation: %v %v %v", result, code, err)
	}
}
