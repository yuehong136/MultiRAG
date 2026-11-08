package deepdoc

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/testsupport"
)

func TestDLAGroupingAndMultipart(t *testing.T) {
	var count atomic.Int32
	server := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/predict" {
			t.Error("wire route changed")
		}
		reader, err := r.MultipartReader()
		if err != nil {
			t.Error(err)
			return
		}
		part, err := reader.NextPart()
		if err != nil {
			t.Error(err)
			return
		}
		data, err := io.ReadAll(part)
		if err != nil {
			t.Error(err)
		}
		if part.FormName() != "request" || part.FileName() != "image.jpg" || part.Header.Get("Content-Type") != "image/jpeg" || string(data) != "JPEG" {
			t.Error("invalid multipart request")
		}
		if count.Add(1) == 1 {
			fmt.Fprint(w, `{"bboxes":[[1,2,3,4,0.9,0],[2,3,4,5,0.8,5]]}`)
		} else {
			fmt.Fprint(w, `{"bboxes":[]}`)
		}
	}))
	client := NewClientWithURL(server.URL)
	result, err := client.DLA(context.Background(), [][]byte{[]byte("JPEG"), []byte("JPEG")})
	if err != nil || len(result) != 2 || len(result[0]) != 2 || len(result[1]) != 0 || result[0][1].Type != "table" {
		t.Fatal(result, err)
	}
	for _, method := range []func(context.Context, [][]byte) ([][]byte, error){client.OCR, client.TSR} {
		if _, err := method(context.Background(), nil); !errors.Is(err, ErrNoRemoteEndpoint) {
			t.Fatal(err)
		}
	}
}
func TestDLAFailuresAreNotEmptyDetections(t *testing.T) {
	var count atomic.Int32
	var status atomic.Int32
	var reply atomic.Value
	reply.Store(`{"bboxes":[[0,0,1,1,0.5,0]]}`)
	status.Store(200)
	server := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		count.Add(1)
		w.WriteHeader(int(status.Load()))
		fmt.Fprint(w, reply.Load().(string))
	}))
	client := NewClientWithURL(server.URL, WithBackoff(0))
	for _, body := range []string{`{}`, `{"bboxes":null}`, `{"bboxes":[[0,0,1]]}`, `{"bboxes":[[0,0,1,1,0.5,99]]}`, `{"bboxes":[[0,0,1,1,0.5,1.5]]}`, `{"bboxes":[[2,0,1,1,0.5,0]]}`, `{"bboxes":[[0,0,1,1,2,0]]}`} {
		reply.Store(body)
		count.Store(0)
		if result, err := client.DLA(context.Background(), [][]byte{[]byte("JPEG")}); result != nil || !errors.Is(err, ErrInvalidResponse) || count.Load() != 3 {
			t.Fatal(body, result, err, count.Load())
		}
	}
	reply.Store("private server details")
	for _, s := range []int32{302, 400, 401, 403, 500} {
		status.Store(s)
		count.Store(0)
		_, err := client.DLA(context.Background(), [][]byte{[]byte("JPEG")})
		want := int32(1)
		if s == 500 {
			want = 3
		}
		if err == nil || count.Load() != want || strings.Contains(err.Error(), "private server details") {
			t.Fatal(s, err, count.Load())
		}
	}
	status.Store(200)
	reply.Store(strings.Repeat("x", maxResponseBytes+1))
	count.Store(0)
	if _, err := client.DLA(context.Background(), [][]byte{[]byte("JPEG")}); err == nil || count.Load() != 1 {
		t.Fatal("oversize response accepted", err, count.Load())
	}
}
func TestDLACancellationAndConfiguration(t *testing.T) {
	testsupport.NewCase(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := NewClientWithURL("http://127.0.0.1:1").DLA(ctx, [][]byte{[]byte("JPEG")}); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	for _, address := range []string{"file:///tmp/data", "http://user:pass@localhost", "http://localhost?secret=1"} {
		client := NewClientWithURL(address)
		if client.Enabled() {
			t.Fatal("invalid URL enabled", address)
		}
		if result, err := client.DLA(context.Background(), [][]byte{[]byte("JPEG")}); err == nil || result != nil {
			t.Fatal(result, err)
		}
	}
	if _, err := NewClientWithURL("").DLA(context.Background(), nil); !errors.Is(err, ErrNoURL) {
		t.Fatal(err)
	}
	if _, err := NewClientWithURL("http://localhost", WithMaxAttempts(0)).DLA(context.Background(), nil); err == nil {
		t.Fatal("invalid attempt count accepted")
	}
	server := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.Copy(io.Discard, r.Body); <-r.Context().Done() }))
	timeoutCtx, stop := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer stop()
	if _, err := NewClientWithURL(server.URL).DLA(timeoutCtx, [][]byte{[]byte("JPEG")}); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
}
