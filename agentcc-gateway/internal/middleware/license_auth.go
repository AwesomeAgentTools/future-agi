package middleware

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/config"
	"github.com/futureagi/agentcc-gateway/internal/models"
	"github.com/futureagi/agentcc-gateway/internal/redisstate"
)

type licenseAuthContextKey struct{}

type licenseClaims struct {
	LicenseID  string   `json:"license_id"`
	InstanceID string   `json:"instance_id"`
	Scope      string   `json:"scope"`
	Services   []string `json:"services"`
	Models     []string `json:"models"`
	ExpiresAt  int64    `json:"exp"`
	JTI        string   `json:"jti"`
}

type jwtHeader struct {
	Algorithm string `json:"alg"`
	KeyID     string `json:"kid"`
}

type modelRequest struct {
	Model string `json:"model"`
}

func LicenseAuth(cfg config.LicenseAuthConfig, store *redisstate.LicenseStore) func(http.Handler) http.Handler {
	keys := buildLicensePublicKeyMap(cfg)
	authEnabled := cfg.Enabled && len(keys) > 0

	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !authEnabled || !strings.HasPrefix(r.URL.Path, "/v1/") {
				next.ServeHTTP(w, r)
				return
			}

			rawToken := extractBearerToken(r)
			if rawToken == "" {
				next.ServeHTTP(w, r)
				return
			}

			claims, err := verifyLicenseToken(rawToken, keys)
			if err != nil {
				next.ServeHTTP(w, r)
				return
			}

			if err := authorizeManagedRequest(r, claims); err != nil {
				models.WriteError(w, models.ErrForbidden(err.Error()))
				return
			}

			if err := authorizeRuntimeState(claims, cfg, store); err != nil {
				models.WriteError(w, err)
				return
			}

			ctx := context.WithValue(r.Context(), licenseAuthContextKey{}, true)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

func authorizeRuntimeState(claims *licenseClaims, cfg config.LicenseAuthConfig, store *redisstate.LicenseStore) *models.APIError {
	if !cfg.RuntimeStateRequired && cfg.RateLimitRPM <= 0 && cfg.MonthlyUsageLimit <= 0 {
		return nil
	}
	if store == nil {
		return models.ErrServiceUnavailable("license runtime state unavailable")
	}

	if cfg.RuntimeStateRequired {
		sessionActive, err := store.SessionActive(claims.JTI)
		if err != nil {
			return models.ErrServiceUnavailable("license runtime state unavailable")
		}
		if !sessionActive {
			return models.ErrForbidden("license session is not active")
		}

		instanceActive, err := store.InstanceActive(claims.LicenseID, claims.InstanceID)
		if err != nil {
			return models.ErrServiceUnavailable("license runtime state unavailable")
		}
		if !instanceActive {
			return models.ErrForbidden("license instance is not active")
		}
	}

	if allowed, err := store.AllowRate(claims.LicenseID, cfg.RateLimitRPM); err != nil {
		return models.ErrServiceUnavailable("license rate state unavailable")
	} else if !allowed {
		return models.ErrTooManyRequests("license rate limit exceeded")
	}

	if allowed, err := store.AllowMonthlyUsage(claims.LicenseID, cfg.MonthlyUsageLimit); err != nil {
		return models.ErrServiceUnavailable("license usage state unavailable")
	} else if !allowed {
		return models.ErrForbidden("license usage limit exceeded")
	}

	return nil
}

func IsLicenseAuthorized(ctx context.Context) bool {
	value, _ := ctx.Value(licenseAuthContextKey{}).(bool)
	return value
}

func buildLicensePublicKeyMap(cfg config.LicenseAuthConfig) map[string]*rsa.PublicKey {
	keys := make(map[string]*rsa.PublicKey)
	if cfg.PublicKey != "" {
		if key, err := parseRSAPublicKey(cfg.PublicKey); err == nil {
			keys["default"] = key
		}
	}
	for _, entry := range cfg.PublicKeys {
		if entry.KID == "" || entry.PublicKey == "" {
			continue
		}
		if key, err := parseRSAPublicKey(entry.PublicKey); err == nil {
			keys[entry.KID] = key
		}
	}
	return keys
}

func verifyLicenseToken(raw string, keys map[string]*rsa.PublicKey) (*licenseClaims, error) {
	parts := strings.Split(raw, ".")
	if len(parts) != 3 {
		return nil, errors.New("invalid token")
	}

	headerBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return nil, err
	}
	var header jwtHeader
	if err := json.Unmarshal(headerBytes, &header); err != nil {
		return nil, err
	}
	if header.Algorithm != "RS256" {
		return nil, errors.New("unsupported license token algorithm")
	}
	keyID := header.KeyID
	if keyID == "" {
		keyID = "default"
	}
	key := keys[keyID]
	if key == nil {
		return nil, errors.New("unknown license token kid")
	}

	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return nil, err
	}
	signed := []byte(parts[0] + "." + parts[1])
	digest := sha256.Sum256(signed)
	if err := rsa.VerifyPKCS1v15(key, crypto.SHA256, digest[:], signature); err != nil {
		return nil, err
	}

	payloadBytes, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, err
	}
	var claims licenseClaims
	if err := json.Unmarshal(payloadBytes, &claims); err != nil {
		return nil, err
	}
	if claims.ExpiresAt <= time.Now().Unix() {
		return nil, errors.New("license token expired")
	}
	if claims.Scope != "enterprise" {
		return nil, errors.New("invalid license token scope")
	}
	return &claims, nil
}

func authorizeManagedRequest(r *http.Request, claims *licenseClaims) error {
	if r.Method != http.MethodPost || r.URL.Path != "/v1/chat/completions" {
		return nil
	}

	model, err := readRequestModel(r)
	if err != nil {
		return err
	}
	service := serviceForModel(model)
	if service == "" {
		return fmt.Errorf("model %q is not a managed FutureAGI model", model)
	}
	if !contains(claims.Services, service) {
		return fmt.Errorf("service %q not included in token scope", service)
	}
	if !contains(claims.Models, model) {
		return fmt.Errorf("model %q not included in token scope", model)
	}
	return nil
}

func readRequestModel(r *http.Request) (string, error) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return "", err
	}
	r.Body = io.NopCloser(bytes.NewReader(body))

	var req modelRequest
	if err := json.Unmarshal(body, &req); err != nil {
		return "", err
	}
	if req.Model == "" {
		return "", errors.New("model is required")
	}
	return req.Model, nil
}

func serviceForModel(model string) string {
	switch {
	case strings.HasPrefix(model, "turing_"):
		return "turing"
	case model == "falcon_ai":
		return "falcon"
	case strings.HasPrefix(model, "protect"):
		return "protect"
	default:
		return ""
	}
}

func parseRSAPublicKey(raw string) (*rsa.PublicKey, error) {
	block, _ := pem.Decode([]byte(raw))
	if block == nil {
		return nil, errors.New("invalid PEM public key")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		pkcs1, pkcs1Err := x509.ParsePKCS1PublicKey(block.Bytes)
		if pkcs1Err != nil {
			return nil, err
		}
		return pkcs1, nil
	}
	key, ok := parsed.(*rsa.PublicKey)
	if !ok {
		return nil, errors.New("public key is not RSA")
	}
	return key, nil
}

func extractBearerToken(r *http.Request) string {
	authHeader := r.Header.Get("Authorization")
	if !strings.HasPrefix(authHeader, "Bearer ") {
		return ""
	}
	return strings.TrimPrefix(authHeader, "Bearer ")
}

func contains(values []string, needle string) bool {
	for _, value := range values {
		if value == needle {
			return true
		}
	}
	return false
}
