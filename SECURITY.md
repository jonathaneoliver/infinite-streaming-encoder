# Security Policy

## Reporting a Vulnerability

If you discover a security issue, please open a GitHub Security Advisory if available
(Security → Advisories → "Report a vulnerability"). If that is not possible, open a
private issue or contact the project owner via GitHub.

Please include:
- A clear description of the issue
- Reproduction steps
- Impact assessment
- Suggested remediation (if known)

We will acknowledge receipt and respond as soon as possible.

## Trust model — read before deploying

This is a control plane that runs privileged local operations. Run it **only on a host and
network you trust**; it is not designed to be exposed to the public internet.

- **Docker socket.** The server mounts `/var/run/docker.sock` so it can spawn and manage
  sibling worker containers. This is **root-equivalent access to the host** — anyone who can
  reach the server's HTTP API can, transitively, control Docker on that host. Do not expose
  the server (default `:8080`) beyond your LAN.
- **AWS credentials.** For cloud encoding the server mounts `~/.aws:ro` and reads AWS
  environment variables. Scope those credentials to the minimum (Batch / Step Functions / S3
  / ECR) needed; never bake long-lived keys into the image.
- **SSH keys.** For multi-box farms the server mounts `~/.ssh:ro` to toggle remote workers.
  Treat any box running the server as able to reach every `DIST_WORKERS` host.
- **MinIO / Temporal.** The local farm publishes MinIO (`:9000`) and Temporal (`:7233`) on
  the master's LAN interface with default development credentials
  (`encoder` / `encoder-secret`). Change these before using on a shared network, and keep the
  ports LAN-only.

Secrets belong in a gitignored `.env` (already excluded) — never commit credentials, PATs,
or per-user URLs.
