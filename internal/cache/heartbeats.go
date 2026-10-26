package cache

import (
	"context"
	"fmt"
	"github.com/redis/go-redis/v9"
	"strconv"
	"time"
)

// TaskExecutorHeartbeats reads a bounded window without changing worker keys.
func (r *RedisClient) TaskExecutorHeartbeats(ctx context.Context) (map[string][]string, error) {
	if r == nil || r.client == nil {
		return nil, fmt.Errorf("redis unavailable")
	}
	ids, err := r.client.SMembers(ctx, "TASKEXE").Result()
	if err != nil {
		return nil, err
	}
	if len(ids) > 256 {
		return nil, fmt.Errorf("too many executors")
	}
	result := map[string][]string{}
	now := time.Now().Unix()
	for _, id := range ids {
		rows, err := r.client.ZRevRangeByScore(ctx, id, &redis.ZRangeBy{Min: strconv.FormatInt(now-1800, 10), Max: strconv.FormatInt(now, 10), Offset: 0, Count: 61}).Result()
		if err != nil {
			return nil, err
		}
		result[id] = rows
	}
	return result, nil
}
