package workflowx

import (
	"context"
	"reflect"
	"sync/atomic"
	"testing"

	"github.com/cloudwego/eino/compose"
)

func TestParallel_RepeatedResumeKeepsCompletedItems(t *testing.T) {
	var calls [3]atomic.Int32
	sub := compose.NewWorkflow[int, int]()
	node := sub.AddLambdaNode("op", compose.InvokableLambda(func(ctx context.Context, in int) (int, error) {
		if was, has, saved := compose.GetInterruptState[int](ctx); was && has {
			in = saved
		}
		count := calls[in].Add(1)
		if count <= int32(in) {
			return 0, compose.StatefulInterrupt(ctx, "wait", in)
		}
		return in + 10, nil
	}))
	node.AddInput(compose.START)
	sub.End().AddInput("op")
	outer := compose.NewWorkflow[[]int, []int]()
	par, err := AddParallelNode(context.Background(), outer, "par", sub)
	if err != nil {
		t.Fatal(err)
	}
	par.AddInput(compose.START)
	outer.End().AddInput("par")
	compiled, err := outer.Compile(context.Background(), compose.WithCheckPointStore(newInMemoryStore()))
	if err != nil {
		t.Fatal(err)
	}
	out, err := compiled.Invoke(context.Background(), []int{0, 1, 2}, compose.WithCheckPointID("repeated-resume"))
	for round := 0; err != nil && round < 6; round++ {
		if _, ok := compose.ExtractInterruptInfo(err); !ok {
			t.Fatal(err)
		}
		ctx := compose.Resume(context.Background(), firstRootInterruptID(t, err))
		out, err = compiled.Invoke(ctx, nil, compose.WithCheckPointID("repeated-resume"))
	}
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(out, []int{10, 11, 12}) {
		t.Fatalf("output=%v", out)
	}
	if calls[0].Load() != 1 || calls[1].Load() != 2 || calls[2].Load() != 3 {
		t.Fatalf("completed items rerun or skipped: %d %d %d", calls[0].Load(), calls[1].Load(), calls[2].Load())
	}
}

func TestParallel_CheckpointCoercionRejectsLossyNumbers(t *testing.T) {
	if _, ok := coerceAnyToO[int](1.25); ok {
		t.Fatal("fraction truncated into integer")
	}
	if _, ok := coerceAnyToO[int32](float64(1 << 40)); ok {
		t.Fatal("overflow accepted")
	}
	if value, ok := coerceAnyToO[int](float64(12)); !ok || value != 12 {
		t.Fatalf("valid checkpoint integer=%v %v", value, ok)
	}
}
