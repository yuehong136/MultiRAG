package redis

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"reflect"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	miniserver "github.com/alicebob/miniredis/v2/server"
	goredis "github.com/redis/go-redis/v9"

	"multirag/internal/server"
	"multirag/internal/testsupport"
)

// Every server has an exclusive listener recorded durably before bind. No
// external Redis instance or shared key is used by these lifecycle tests.
func newRegisteredRedis(t *testing.T) (*miniredis.Miniredis, *testsupport.CaseJournal) {
	t.Helper()
	journal := testsupport.NewCase(t)
	for range 30 {
		var value [2]byte
		if _, err := rand.Read(value[:]); err != nil {
			t.Fatal(err)
		}
		address := fmt.Sprintf("127.0.0.1:%d", 20000+int(binary.BigEndian.Uint16(value[:]))%30000)
		journal.Event("reserve-redis-listener", map[string]any{"address": address})
		instance := miniredis.NewMiniRedis()
		if err := instance.StartAddr(address); err != nil {
			journal.Event("redis-bind-failed", map[string]any{"address": address})
			continue
		}
		journal.Event("redis-listener-created", map[string]any{"address": address})
		t.Cleanup(func() {
			instance.Close()
			connection, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
			if connection != nil {
				_ = connection.Close()
				t.Error("Redis listener survived cleanup")
			}
			journal.Event("redis-listener-cleaned", map[string]any{"address": address, "absent": err != nil})
		})
		return instance, journal
	}
	t.Fatal("no exclusive Redis test address available")
	return nil, nil
}

func redisTestConfig(t *testing.T, instance *miniredis.Miniredis) server.RedisConfig {
	t.Helper()
	host, port, err := net.SplitHostPort(instance.Addr())
	if err != nil {
		t.Fatal(err)
	}
	number, err := strconv.Atoi(port)
	if err != nil {
		t.Fatal(err)
	}
	return server.RedisConfig{Host: host, Port: number}
}

func resetRedisLifecycle(t *testing.T) {
	t.Helper()
	if err := Close(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := Close(); err != nil {
			t.Error(err)
		}
	})
}

func waitForRedisConnections(t *testing.T, instance *miniredis.Miniredis, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if instance.CurrentConnectionCount() == want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("Redis connections=%d, want %d", instance.CurrentConnectionCount(), want)
}

func TestInitRejectsInvalidConfiguration(t *testing.T) {
	resetRedisLifecycle(t)
	for _, config := range []*server.RedisConfig{
		nil,
		{},
		{Host: " ", Port: 6379},
		{Host: " localhost", Port: 6379},
		{Host: "redis://localhost", Port: 6379},
		{Host: "localhost", Port: 0},
		{Host: "localhost", Port: 65536},
		{Host: "localhost", Port: 6379, DB: -1},
	} {
		if err := Init(config); err == nil {
			t.Fatalf("invalid configuration succeeded: %+v", config)
		}
		if Get() != nil || IsEnabled() {
			t.Fatal("invalid configuration published a client")
		}
	}
	if NewDistributedLock("lock", "value", time.Second, 0) != nil || NewTokenBucket("bucket", 1, 1) != nil {
		t.Fatal("uninitialized constructors acquired a client")
	}
}

func TestInitFailureRetryCloseAndReinitialize(t *testing.T) {
	instance, journal := newRegisteredRedis(t)
	resetRedisLifecycle(t)
	config := redisTestConfig(t, instance)
	instance.SetError("ERR intentional initialization rejection")
	if err := Init(&config); err == nil {
		t.Fatal("failed server initialization succeeded")
	}
	if Get() != nil || IsEnabled() {
		t.Fatal("failed candidate was published")
	}
	waitForRedisConnections(t, instance, 0)
	journal.Event("failed-redis-candidate-closed", map[string]any{"connections": instance.CurrentConnectionCount()})
	instance.SetError("")
	if err := Init(&config); err != nil {
		t.Fatalf("initialization failure poisoned retry: %v", err)
	}
	first := Get()
	if first == nil || !IsEnabled() || first.GetClient() == nil {
		t.Fatal("connected client not published")
	}
	if err := Init(&config); err != nil || Get() != first {
		t.Fatalf("repeated initialization replaced the active client: %v", err)
	}
	config.DB = 1
	if first.config.DB != 0 {
		t.Fatal("published configuration aliases caller memory")
	}
	if err := Init(nil); err == nil || Get() != first {
		t.Fatal("invalid initialization must fail without replacing active client")
	}
	lock := NewDistributedLock("lock", "value", time.Second, 0)
	bucket := NewTokenBucket("bucket", 1, 1)
	if lock == nil || lock.client != first || bucket == nil || bucket.client != first {
		t.Fatal("constructors did not retain the published snapshot")
	}
	if err := Close(); err != nil {
		t.Fatal(err)
	}
	if Get() != nil || IsEnabled() {
		t.Fatal("closed client is still published")
	}
	if err := Close(); err != nil {
		t.Fatalf("repeated Close is not idempotent: %v", err)
	}
	if err := first.PingContext(context.Background()); !errors.Is(err, goredis.ErrClosed) {
		t.Fatalf("old snapshot connection pool remains open: %v", err)
	}
	waitForRedisConnections(t, instance, 0)
	if err := Init(&config); err != nil {
		t.Fatalf("initialization after Close failed: %v", err)
	}
	if Get() == first || Get().config.DB != 1 || lock.client != first || bucket.client != first {
		t.Fatal("reinitialization reused or mutated a prior snapshot")
	}
}

func TestConcurrentRedisLifecycleSnapshots(t *testing.T) {
	instance, _ := newRegisteredRedis(t)
	resetRedisLifecycle(t)
	config := redisTestConfig(t, instance)
	const workers, iterations = 8, 12
	errors := make(chan error, 2*workers*iterations)
	var group sync.WaitGroup
	for worker := range workers {
		group.Add(1)
		go func() {
			defer group.Done()
			for iteration := range iterations {
				var err error
				if (worker+iteration)%3 == 0 {
					err = Close()
				} else {
					err = Init(&config)
				}
				if err != nil {
					errors <- err
				}
				if snapshot := Get(); snapshot != nil && snapshot.GetClient() == nil {
					errors <- fmt.Errorf("partially initialized snapshot")
				}
				_ = IsEnabled()
				_ = NewDistributedLock("lock", "value", time.Second, 0)
				_ = NewTokenBucket("bucket", 1, 1)
			}
		}()
	}
	group.Wait()
	close(errors)
	for err := range errors {
		t.Error(err)
	}
	if err := Close(); err != nil {
		t.Fatal(err)
	}
	waitForRedisConnections(t, instance, 0)
	if Get() != nil || IsEnabled() {
		t.Fatal("concurrent lifecycle left a published client")
	}
}

func TestPingContextDeadlineInterruptsWarmedSocket(t *testing.T) {
	instance, journal := newRegisteredRedis(t)
	resetRedisLifecycle(t)
	config := redisTestConfig(t, instance)
	if err := Init(&config); err != nil {
		t.Fatal(err)
	}
	client := Get()
	if err := client.PingContext(context.Background()); err != nil {
		t.Fatal(err)
	}
	warmedConnections := instance.TotalConnectionCount()
	entered, release := make(chan struct{}), make(chan struct{})
	var enteredOnce, releaseOnce sync.Once
	unblock := func() { releaseOnce.Do(func() { close(release) }) }
	defer unblock()
	// The existing pooled TCP connection reaches the server, then the server
	// deliberately withholds its reply. A pre-cancelled context or failed dial
	// cannot satisfy this regression. Removing ContextTimeoutEnabled leaves
	// the socket blocked until the 3-second Redis read timeout and fails it.
	instance.Server().SetPreHook(func(_ *miniserver.Peer, command string, _ ...string) bool {
		if command == "PING" {
			enteredOnce.Do(func() { close(entered) })
			<-release
		}
		return false
	})
	ctx, cancel := context.WithTimeout(context.Background(), 250*time.Millisecond)
	defer cancel()
	started := time.Now()
	result := make(chan error, 1)
	go func() { result <- client.PingContext(ctx) }()
	select {
	case <-entered:
		if ctx.Err() != nil {
			unblock()
			<-result
			t.Fatal("deadline elapsed before the warmed socket reached the server")
		}
	case <-time.After(time.Second):
		unblock()
		<-result
		t.Fatal("PING did not reach the warmed socket")
	}
	var commandErr error
	timedOut := false
	select {
	case commandErr = <-result:
	case <-time.After(time.Second):
		timedOut = true
		unblock()
		commandErr = <-result
	}
	elapsed := time.Since(started)
	unblock()
	if timedOut || elapsed > time.Second {
		t.Fatalf("context deadline did not interrupt socket read: elapsed=%s error=%v", elapsed, commandErr)
	}
	var networkError net.Error
	if commandErr == nil || (!errors.Is(commandErr, context.DeadlineExceeded) && (!errors.As(commandErr, &networkError) || !networkError.Timeout())) {
		t.Fatalf("want a real socket/context deadline error, got %v", commandErr)
	}
	if instance.TotalConnectionCount() != warmedConnections {
		t.Fatal("deadline regression used a new connection instead of the warmed socket")
	}
	journal.Event("redis-warmed-socket-deadline-verified", map[string]any{"elapsed_ms": elapsed.Milliseconds(), "deadline_ms": 250, "connections": warmedConnections})
}

type redisContents struct {
	keys []string
	dump string
	ttls map[string]time.Duration
}

func snapshotRedisContents(instance *miniredis.Miniredis) redisContents {
	keys := instance.Keys()
	ttls := make(map[string]time.Duration, len(keys))
	for _, key := range keys {
		ttls[key] = instance.TTL(key)
	}
	// Every fixture value is shorter than miniredis's dump truncation limit.
	return redisContents{keys: keys, dump: instance.Dump(), ttls: ttls}
}

func TestTaskExecutorHeartbeatsReadOnlyBounds(t *testing.T) {
	instance, journal := newRegisteredRedis(t)
	resetRedisLifecycle(t)
	config := redisTestConfig(t, instance)
	if err := Init(&config); err != nil {
		t.Fatal(err)
	}
	client := Get()
	fixtureKeys := []string{"TASKEXE", "worker-many", "worker-window", "unrelated"}
	journal.Event("reserve-redis-heartbeat-fixture-before-write", map[string]any{"keys": fixtureKeys})
	if _, err := instance.SAdd("TASKEXE", "worker-many", "worker-window"); err != nil {
		t.Fatal(err)
	}
	if err := instance.Set("unrelated", "preserve this value"); err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	wantMany := make([]string, 61)
	for i := range 70 {
		member := fmt.Sprintf("heartbeat-%02d", i)
		if _, err := instance.ZAdd("worker-many", float64(now-int64(i)), member); err != nil {
			t.Fatal(err)
		}
		if i < len(wantMany) {
			wantMany[i] = member
		}
	}
	for _, row := range []struct {
		worker, member string
		score          int64
	}{
		{"worker-many", "too old", now - 1810},
		{"worker-many", "future", now + 60},
		{"worker-window", "inside 30 minutes", now - 1790},
		{"worker-window", "outside 30 minutes", now - 1810},
		{"worker-window", "future", now + 60},
	} {
		if _, err := instance.ZAdd(row.worker, float64(row.score), row.member); err != nil {
			t.Fatal(err)
		}
	}
	for i, key := range fixtureKeys {
		instance.SetTTL(key, time.Duration(i+1)*time.Hour)
	}
	assertReadOnly := func(read func()) {
		t.Helper()
		before := snapshotRedisContents(instance)
		read()
		after := snapshotRedisContents(instance)
		if !reflect.DeepEqual(before, after) {
			t.Fatalf("probe mutated keys, values, scores, members or TTLs: before=%+v after=%+v", before, after)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	assertReadOnly(func() {
		if err := client.PingContext(ctx); err != nil {
			t.Fatal(err)
		}
		rows, err := client.TaskExecutorHeartbeats(ctx)
		if err != nil || len(rows) != 2 || !reflect.DeepEqual(rows["worker-many"], wantMany) || !reflect.DeepEqual(rows["worker-window"], []string{"inside 30 minutes"}) {
			t.Fatalf("heartbeat window/count mismatch: rows=%v err=%v", rows, err)
		}
	})
	// Exactly 256 executors remain readable. The additional executor IDs have
	// no sorted-set keys; read-only probes must not create those missing keys.
	journal.Event("reserve-redis-heartbeat-members-before-write", map[string]any{"key": "TASKEXE", "executor_count": 256})
	for i := range 254 {
		if _, err := instance.SAdd("TASKEXE", fmt.Sprintf("empty-worker-%03d", i)); err != nil {
			t.Fatal(err)
		}
	}
	assertReadOnly(func() {
		rows, err := client.TaskExecutorHeartbeats(ctx)
		if err != nil || len(rows) != 256 {
			t.Fatalf("256-executor boundary: rows=%d err=%v", len(rows), err)
		}
	})
	journal.Event("reserve-redis-heartbeat-members-before-write", map[string]any{"key": "TASKEXE", "executor_count": 257})
	if _, err := instance.SAdd("TASKEXE", "executor-over-limit"); err != nil {
		t.Fatal(err)
	}
	assertReadOnly(func() {
		rows, err := client.TaskExecutorHeartbeats(ctx)
		if err == nil || rows != nil {
			t.Fatalf("257 executors must fail strongly: rows=%v err=%v", rows, err)
		}
	})
	journal.Event("redis-heartbeat-read-only-bounds-verified", map[string]any{"keys": instance.Keys(), "max_rows": 61, "max_executors": 256, "overflow_rejected": true, "values_and_ttls_unchanged": true, "ping_created_no_health_key": true})
}
