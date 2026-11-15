package models

import (
	"context"

	"testing"
)

func TestBuiltInMediaCapabilitiesAreExplicit(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := (&APIConfig{Context: ctx}).RequestContext().Err(); err == nil {
		t.Fatal("cancel lost")
	}
}
