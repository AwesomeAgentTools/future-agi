package redisstate

import (
	"context"
	"fmt"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

type LicenseStore struct {
	client *Client
}

func NewLicenseStore(client *Client) *LicenseStore {
	return &LicenseStore{client: client}
}

func (s *LicenseStore) SessionActive(jti string) (bool, error) {
	if s == nil || s.client == nil {
		return false, fmt.Errorf("license redis store unavailable")
	}
	var value string
	err := s.client.Do(func(rdb redis.UniversalClient) error {
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		defer cancel()
		result, err := rdb.Get(ctx, "license_session:"+jti).Result()
		if err != nil {
			return err
		}
		value = result
		return nil
	})
	if err != nil {
		return false, err
	}
	return value == "active", nil
}

func (s *LicenseStore) InstanceActive(licenseID, instanceID string) (bool, error) {
	if s == nil || s.client == nil {
		return false, fmt.Errorf("license redis store unavailable")
	}
	key := "license_instance:" + licenseID + ":" + instanceID
	var exists bool
	err := s.client.Do(func(rdb redis.UniversalClient) error {
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		defer cancel()
		count, err := rdb.Exists(ctx, key).Result()
		if err != nil {
			return err
		}
		exists = count > 0
		return nil
	})
	if err != nil {
		return false, err
	}
	return exists, nil
}

func (s *LicenseStore) AllowRate(licenseID string, limit int) (bool, error) {
	if limit <= 0 {
		return true, nil
	}
	if s == nil || s.client == nil {
		return false, fmt.Errorf("license redis store unavailable")
	}
	window := time.Now().Unix() / 60
	key := "license_rate:" + licenseID + ":" + strconv.FormatInt(window, 10)
	var count int64
	err := s.client.Do(func(rdb redis.UniversalClient) error {
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		defer cancel()
		pipe := rdb.TxPipeline()
		incr := pipe.Incr(ctx, key)
		pipe.Expire(ctx, key, 2*time.Minute)
		_, err := pipe.Exec(ctx)
		if err != nil {
			return err
		}
		count = incr.Val()
		return nil
	})
	if err != nil {
		return false, err
	}
	return count <= int64(limit), nil
}

func (s *LicenseStore) AllowMonthlyUsage(licenseID string, limit int) (bool, error) {
	if limit <= 0 {
		return true, nil
	}
	if s == nil || s.client == nil {
		return false, fmt.Errorf("license redis store unavailable")
	}
	month := time.Now().UTC().Format("2006-01")
	key := "license_usage:" + licenseID + ":" + month
	var count int64
	err := s.client.Do(func(rdb redis.UniversalClient) error {
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		defer cancel()
		pipe := rdb.TxPipeline()
		incr := pipe.Incr(ctx, key)
		pipe.Expire(ctx, key, 45*24*time.Hour)
		_, err := pipe.Exec(ctx)
		if err != nil {
			return err
		}
		count = incr.Val()
		return nil
	})
	if err != nil {
		return false, err
	}
	return count <= int64(limit), nil
}
