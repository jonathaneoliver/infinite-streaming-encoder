// Package imageinfo fetches OCI image labels from a remote registry
// (currently GHCR) so the SPA can show the cloud image's version and
// git revision next to the local binary's. Read-only, anonymous — the
// encoder image is public.
package imageinfo

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

type Info struct {
	Image    string `json:"image"`    // "ghcr.io/jonathaneoliver/encoder:latest"
	Version  string `json:"version"`  // from org.opencontainers.image.version label
	Revision string `json:"revision"` // from org.opencontainers.image.revision label
	Digest   string `json:"digest"`   // manifest digest (sha256:...)
	Error    string `json:"error,omitempty"`
}

type cacheEntry struct {
	info    Info
	fetched time.Time
}

type Client struct {
	http  *http.Client
	mu    sync.Mutex
	cache map[string]cacheEntry
	ttl   time.Duration
	// GHCR_PAT — required when the package is private. Sent as Basic
	// auth on the token exchange; the resulting bearer token carries
	// pull scope. If unset, anonymous is attempted (works only for
	// public packages).
	ghcrPAT      string
	ghcrUsername string
}

func NewClient(ghcrUsername, ghcrPAT string) *Client {
	return &Client{
		http:         &http.Client{Timeout: 10 * time.Second},
		cache:        map[string]cacheEntry{},
		ttl:          60 * time.Second,
		ghcrPAT:      ghcrPAT,
		ghcrUsername: ghcrUsername,
	}
}

// Get returns the cached Info for `image` or fetches fresh. Never
// returns an error — lookup failures become Info.Error so the UI can
// surface them inline instead of a failed API call.
func (c *Client) Get(ctx context.Context, image string) Info {
	c.mu.Lock()
	if e, ok := c.cache[image]; ok && time.Since(e.fetched) < c.ttl {
		c.mu.Unlock()
		return e.info
	}
	c.mu.Unlock()

	info := c.fetch(ctx, image)

	c.mu.Lock()
	c.cache[image] = cacheEntry{info: info, fetched: time.Now()}
	c.mu.Unlock()
	return info
}

func (c *Client) fetch(ctx context.Context, image string) Info {
	info := Info{Image: image}
	reg, repo, tag, err := parseImage(image)
	if err != nil {
		info.Error = err.Error()
		return info
	}
	if reg != "ghcr.io" {
		info.Error = "only ghcr.io is supported"
		return info
	}

	token, err := c.anonymousToken(ctx, repo)
	if err != nil {
		info.Error = "token: " + err.Error()
		return info
	}

	manifestDigest, configDigest, err := c.resolveManifest(ctx, token, repo, tag)
	if err != nil {
		info.Error = "manifest: " + err.Error()
		return info
	}
	info.Digest = manifestDigest

	labels, err := c.fetchConfigLabels(ctx, token, repo, configDigest)
	if err != nil {
		info.Error = "config: " + err.Error()
		return info
	}
	info.Version = labels["org.opencontainers.image.version"]
	info.Revision = labels["org.opencontainers.image.revision"]
	return info
}

// parseImage splits "ghcr.io/jonathaneoliver/encoder:v0.1.0" into
// registry, repo, tag. Defaults tag to "latest" when absent.
func parseImage(image string) (registry, repo, tag string, err error) {
	slash := strings.Index(image, "/")
	if slash < 0 {
		return "", "", "", fmt.Errorf("bad image ref: %s", image)
	}
	registry = image[:slash]
	rest := image[slash+1:]
	tag = "latest"
	if colon := strings.LastIndex(rest, ":"); colon >= 0 {
		tag = rest[colon+1:]
		rest = rest[:colon]
	}
	return registry, rest, tag, nil
}

// anonymousToken asks GHCR for a pull-scope bearer token. For public
// packages no credentials are needed; for private packages we send
// GHCR_PAT as Basic auth, and GHCR echoes back a scoped bearer token
// we can use on manifest/blob fetches.
func (c *Client) anonymousToken(ctx context.Context, repo string) (string, error) {
	u := fmt.Sprintf("https://ghcr.io/token?service=ghcr.io&scope=repository:%s:pull", repo)
	req, _ := http.NewRequestWithContext(ctx, "GET", u, nil)
	if c.ghcrPAT != "" {
		user := c.ghcrUsername
		if user == "" {
			user = "x-access-token" // GitHub's convention for PAT-as-password
		}
		req.SetBasicAuth(user, c.ghcrPAT)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("%d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var tok struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&tok); err != nil {
		return "", err
	}
	return tok.Token, nil
}

// resolveManifest returns (manifestDigest, configDigest). For
// multi-arch images the top-level manifest is an index; we drill into
// the linux/amd64 entry since that matches our default c7i / c7a
// instance families. Graviton users running the same tag would see
// the same version label (same build), so amd64 is a safe sentinel.
func (c *Client) resolveManifest(ctx context.Context, token, repo, tag string) (string, string, error) {
	const accept = "application/vnd.oci.image.index.v1+json," +
		"application/vnd.oci.image.manifest.v1+json," +
		"application/vnd.docker.distribution.manifest.list.v2+json," +
		"application/vnd.docker.distribution.manifest.v2+json"

	body, digest, mediaType, err := c.getManifest(ctx, token, repo, tag, accept)
	if err != nil {
		return "", "", err
	}

	switch {
	case strings.Contains(mediaType, "image.index") || strings.Contains(mediaType, "manifest.list"):
		var idx struct {
			Manifests []struct {
				Digest   string `json:"digest"`
				Platform struct {
					Architecture string `json:"architecture"`
					OS           string `json:"os"`
				} `json:"platform"`
			} `json:"manifests"`
		}
		if err := json.Unmarshal(body, &idx); err != nil {
			return "", "", err
		}
		var amdDigest string
		for _, m := range idx.Manifests {
			if m.Platform.OS == "linux" && m.Platform.Architecture == "amd64" {
				amdDigest = m.Digest
				break
			}
		}
		if amdDigest == "" && len(idx.Manifests) > 0 {
			amdDigest = idx.Manifests[0].Digest
		}
		if amdDigest == "" {
			return "", "", fmt.Errorf("index has no manifests")
		}
		// Recurse: fetch the per-arch manifest by digest.
		body, _, _, err = c.getManifest(ctx, token, repo, amdDigest, accept)
		if err != nil {
			return "", "", err
		}
		digest = amdDigest
		fallthrough
	default:
		var mf struct {
			Config struct {
				Digest string `json:"digest"`
			} `json:"config"`
		}
		if err := json.Unmarshal(body, &mf); err != nil {
			return "", "", err
		}
		if mf.Config.Digest == "" {
			return "", "", fmt.Errorf("manifest has no config digest")
		}
		return digest, mf.Config.Digest, nil
	}
}

func (c *Client) getManifest(ctx context.Context, token, repo, ref, accept string) ([]byte, string, string, error) {
	u := fmt.Sprintf("https://ghcr.io/v2/%s/manifests/%s", repo, ref)
	req, _ := http.NewRequestWithContext(ctx, "GET", u, nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", accept)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, "", "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return nil, "", "", fmt.Errorf("%d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, "", "", err
	}
	return body, resp.Header.Get("Docker-Content-Digest"), resp.Header.Get("Content-Type"), nil
}

func (c *Client) fetchConfigLabels(ctx context.Context, token, repo, configDigest string) (map[string]string, error) {
	u := fmt.Sprintf("https://ghcr.io/v2/%s/blobs/%s", repo, configDigest)
	req, _ := http.NewRequestWithContext(ctx, "GET", u, nil)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("%d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var cfg struct {
		Config struct {
			Labels map[string]string `json:"Labels"`
		} `json:"config"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&cfg); err != nil {
		return nil, err
	}
	return cfg.Config.Labels, nil
}
