package otel

import (
	"context"
	"io"
	"math"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"multirag/internal/testsupport"
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
		{SampleRatio: math.NaN()}, {SampleRatio: math.Inf(1)}, {SampleRatio: math.Inf(-1)}, {SampleRatio: 2},
	} {
		if provider, err := NewTracerProvider(context.Background(), cfg); err == nil {
			provider.Shutdown(context.Background())
			t.Fatalf("invalid config accepted: %+v", cfg)
		}
	}
}

func TestProviderOriginalEndpointAndNegativeDisable(t *testing.T) {
	prior := otel.GetTracerProvider()
	t.Cleanup(func() { otel.SetTracerProvider(prior) })
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	disabled, err := NewTracerProvider(ctx, ProviderConfig{SampleRatio: -1})
	if err != nil {
		t.Fatal("original negative sampling disables export", err)
	}
	_, dropped := disabled.Tracer("disabled").Start(ctx, "disabled")
	if dropped.IsRecording() {
		t.Fatal("negative sampling recorded a span")
	}
	dropped.End()
	if err := disabled.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	collector := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, readErr := io.ReadAll(r.Body)
		if readErr != nil || r.Method != http.MethodPost || r.URL.Path != "/v1/traces" || r.Header.Get("Content-Type") != "application/x-protobuf" || len(data) == 0 {
			t.Error("original OTLP/HTTP collector request", readErr, r.Method, r.URL.Path)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		calls.Add(1)
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK)
	}))
	provider, err := NewTracerProvider(ctx, ProviderConfig{SampleRatio: 1, OTLPEndpoint: strings.TrimPrefix(collector.URL, "http://"), Insecure: true})
	if err != nil {
		t.Fatal(err)
	}
	defer provider.Shutdown(ctx)
	_, span := provider.Tracer("actual-http").Start(ctx, "owned-span")
	span.End()
	if err := provider.ForceFlush(ctx); err != nil || calls.Load() != 1 {
		t.Fatal("original bare endpoint did not export", err, calls.Load())
	}
}
