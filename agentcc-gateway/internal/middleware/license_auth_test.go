package middleware

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/config"
)

func TestLicenseAuthAcceptsScopedManagedToken(t *testing.T) {
	privateKey, publicPEM := testRSAKeyPair(t)
	token := signTestLicenseToken(t, privateKey, licenseClaims{
		LicenseID:  "lic_test",
		InstanceID: "inst_test",
		Scope:      "enterprise",
		Services:   []string{"turing"},
		Models:     []string{"turing_small"},
		ExpiresAt:  time.Now().Add(time.Hour).Unix(),
		JTI:        "tok_test",
	})

	called := false
	handler := LicenseAuth(config.LicenseAuthConfig{Enabled: true, PublicKey: publicPEM}, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		if !IsLicenseAuthorized(r.Context()) {
			t.Fatal("request context was not marked license-authorized")
		}
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"turing_small"}`))
	req.Header.Set("Authorization", "Bearer "+token)
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)

	if !called {
		t.Fatal("handler was not called")
	}
	if rr.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", rr.Code)
	}
}

func TestLicenseAuthRejectsModelOutsideScope(t *testing.T) {
	privateKey, publicPEM := testRSAKeyPair(t)
	token := signTestLicenseToken(t, privateKey, licenseClaims{
		LicenseID:  "lic_test",
		InstanceID: "inst_test",
		Scope:      "enterprise",
		Services:   []string{"turing"},
		Models:     []string{"turing_small"},
		ExpiresAt:  time.Now().Add(time.Hour).Unix(),
		JTI:        "tok_test",
	})

	handler := LicenseAuth(config.LicenseAuthConfig{Enabled: true, PublicKey: publicPEM}, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler should not be called")
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"turing_large"}`))
	req.Header.Set("Authorization", "Bearer "+token)
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rr.Code)
	}
}

func TestLicenseAuthRejectsExpiredTokenForManagedModel(t *testing.T) {
	privateKey, publicPEM := testRSAKeyPair(t)
	token := signTestLicenseToken(t, privateKey, licenseClaims{
		LicenseID:  "lic_test",
		InstanceID: "inst_test",
		Scope:      "enterprise",
		Services:   []string{"turing"},
		Models:     []string{"turing_small"},
		ExpiresAt:  time.Now().Add(-time.Minute).Unix(),
		JTI:        "tok_test",
	})

	handler := LicenseAuth(config.LicenseAuthConfig{Enabled: true, PublicKey: publicPEM}, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler should not be called")
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"turing_small"}`))
	req.Header.Set("Authorization", "Bearer "+token)
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rr.Code)
	}
}

func TestLicenseAuthRejectsMissingTokenForManagedModel(t *testing.T) {
	_, publicPEM := testRSAKeyPair(t)
	handler := LicenseAuth(config.LicenseAuthConfig{Enabled: true, PublicKey: publicPEM}, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler should not be called")
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"turing_small"}`))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rr.Code)
	}
}

func TestLicenseAuthRejectsInvalidTokenForManagedModel(t *testing.T) {
	_, publicPEM := testRSAKeyPair(t)
	handler := LicenseAuth(config.LicenseAuthConfig{Enabled: true, PublicKey: publicPEM}, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler should not be called")
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"protect_flash"}`))
	req.Header.Set("Authorization", "Bearer not-a-valid-jwt")
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rr.Code)
	}
}

func TestLicenseAuthFailsClosedWhenRuntimeStateRequiredWithoutStore(t *testing.T) {
	privateKey, publicPEM := testRSAKeyPair(t)
	token := signTestLicenseToken(t, privateKey, licenseClaims{
		LicenseID:  "lic_test",
		InstanceID: "inst_test",
		Scope:      "enterprise",
		Services:   []string{"turing"},
		Models:     []string{"turing_small"},
		ExpiresAt:  time.Now().Add(time.Hour).Unix(),
		JTI:        "tok_test",
	})

	handler := LicenseAuth(config.LicenseAuthConfig{
		Enabled:              true,
		PublicKey:            publicPEM,
		RuntimeStateRequired: true,
	}, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("handler should not be called")
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"turing_small"}`))
	req.Header.Set("Authorization", "Bearer "+token)
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d", rr.Code)
	}
}

func testRSAKeyPair(t *testing.T) (*rsa.PrivateKey, string) {
	t.Helper()
	privateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	publicDER, err := x509.MarshalPKIXPublicKey(&privateKey.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	publicPEM := string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: publicDER}))
	return privateKey, publicPEM
}

func signTestLicenseToken(t *testing.T, privateKey *rsa.PrivateKey, claims licenseClaims) string {
	t.Helper()
	headerBytes, _ := json.Marshal(jwtHeader{Algorithm: "RS256", KeyID: "default"})
	payloadBytes, _ := json.Marshal(claims)
	header := base64.RawURLEncoding.EncodeToString(headerBytes)
	payload := base64.RawURLEncoding.EncodeToString(payloadBytes)
	signed := []byte(header + "." + payload)
	digest := sha256.Sum256(signed)
	sig, err := rsa.SignPKCS1v15(rand.Reader, privateKey, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return header + "." + payload + "." + base64.RawURLEncoding.EncodeToString(sig)
}
