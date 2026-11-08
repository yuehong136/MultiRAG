package otel

import (
	"context"
	"math"
	"testing"
)

func TestProviderDefaultsToNoExport(t *testing.T) {
	provider, err := NewTracerProvider(context.Background(), ProviderConfig{})
	if err != nil {
		t.Fatal(err)
	}
	defer provider.Shutdown(context.Background())
	_, span := provider.Tracer("test").Start(context.Background(), "noop")
	defer span.End()
	if span.IsRecording() {
		t.Fatal("zero config must not record")
	}
}
func TestProviderRejectsInvalidConfiguration(t *testing.T) {
	for _, cfg := range []ProviderConfig{
		{SampleRatio: math.NaN()}, {SampleRatio: -1}, {SampleRatio: 2},
		{SampleRatio: 1, OTLPEndpoint: "collector:4318"},
		{SampleRatio: 1, OTLPEndpoint: "http://collector:4318"},
		{SampleRatio: 1, OTLPEndpoint: "https://user:password@collector:4318"},
	} {
		if provider, err := NewTracerProvider(context.Background(), cfg); err == nil {
			provider.Shutdown(context.Background())
			t.Fatalf("invalid config accepted: %+v", cfg)
		}
	}
}
