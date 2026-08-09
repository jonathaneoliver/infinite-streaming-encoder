package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/api"
	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/awswatch"
	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/diststage"
	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/tmpstage"
	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/watcher"
)

// Version, gitSha, and imageTag are stamped at build time via
// `-ldflags "-X main.version=... -X main.gitSha=... -X main.imageTag=..."`.
// The Makefile reads ./VERSION and `git rev-parse --short HEAD` (gitSha =
// real HEAD) and the content hash (imageTag); unstamped builds (plain
// `go build`) fall back to the defaults below.
var (
	version  = "dev"
	gitSha   = "unknown"
	imageTag = "unknown"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func intEnv(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func main() {
	addr := flag.String("addr", env("LISTEN_ADDR", ":8080"), "listen address")
	sourceDir := flag.String("source-dir", env("SOURCE_DIR", "/media/originals"), "source video directory")
	outputDir := flag.String("output-dir", env("OUTPUT_DIR", "/media/dynamic_content"), "encode output directory")
	tmpDir := flag.String("tmp-dir", env("TMP_DIR", "/media/tmp"), "temporary directory for in-progress encodes")
	scriptsDir := flag.String("scripts-dir", env("SCRIPTS_DIR", "/scripts"), "directory containing encode scripts")
	// No personal-namespace fallback (#149): a fork would otherwise silently
	// report and pass the upstream author's image. Empty degrades gracefully —
	// the About tab omits the cloud image block rather than showing a wrong one.
	dockerImage := flag.String("docker-image", env("DOCKER_IMAGE", ""), "Docker image used for remote (EC2) encodes")
	// The cloud worker image is an ECR ref, but the About tab reads OCI labels
	// only from GHCR. `publish` keeps ECR + GHCR in sync by tag, so we look up
	// the version/commit from the GHCR twin at the same tag.
	ghcrImage := flag.String("ghcr-image", env("GHCR_IMAGE", ""), "GHCR image (no tag) used to resolve the cloud image's version labels; derived from GHCR_ORG by the Makefile")
	// Host-side paths for bind-mounting into sibling worker containers.
	// `docker run -v` from inside a container resolves paths against the
	// host daemon, so the in-container paths above aren't usable there.
	hostSourceDir := flag.String("host-source-dir", env("HOST_SOURCE_DIR", ""), "host path for SourceDir (for worker container mounts)")
	hostOutputDir := flag.String("host-output-dir", env("HOST_OUTPUT_DIR", ""), "host path for OutputDir")
	hostTmpDir := flag.String("host-tmp-dir", env("HOST_TMP_DIR", ""), "host path for TmpDir")
	hostAWSDir := flag.String("host-aws-dir", env("HOST_AWS_DIR", ""), "host path for ~/.aws (cloud jobs only)")
	encoderImage := flag.String("encoder-image", env("ENCODER_IMAGE", "encoder:latest"), "image used for worker containers")
	stateMachineArn := flag.String("state-machine-arn", env("STATE_MACHINE_ARN", ""), "Step Functions state machine ARN for the cloud-batch target (empty disables that target)")
	autoWatch := flag.Bool("auto-watch", env("AUTO_WATCH", "true") == "true", "auto-encode new files in source dir")
	watchInterval := flag.Duration("watch-interval", 30*time.Second, "filesystem watch polling interval")
	defaultTarget := flag.String("default-target", env("DEFAULT_TARGET", "local"), "default encode target: local or cloud")
	defaultCodec := flag.String("default-codec", env("DEFAULT_CODEC", "both"), "default codec: h264, hevc, both")
	defaultLadder := flag.String("default-ladder", env("DEFAULT_LADDER", encode.DefaultLadderName), "default encoding ladder profile")
	defaultMaxRes := flag.String("default-max-res", env("DEFAULT_MAX_RES", ""), "default max resolution (empty = no limit)")
	defaultMinRes := flag.String("default-min-res", env("DEFAULT_MIN_RES", ""), "default min resolution (empty = no floor)")
	maxConcurrent := flag.Int("max-concurrent", intEnv("MAX_CONCURRENT", 1), "max concurrent encode jobs")
	// AWS watchdog (issue #5 phase 5). Polls `infinite_streaming_encoder.cloud.inventory` on
	// an interval; any instance older than the lifetime budget is flagged
	// as a leak and, when AUTO_TERMINATE_STALE=true, force-terminated.
	awsWatchInterval := flag.Duration("aws-watch-interval", 60*time.Second, "AWS inventory poll interval; 0 disables")
	awsMaxLifetime := flag.Duration("aws-max-lifetime", 4*time.Hour, "terminate EC2 instances older than this")
	awsAutoTerminate := flag.Bool("aws-auto-terminate-stale",
		env("AUTO_TERMINATE_STALE", "true") == "true",
		"force-terminate stale instances (false = warn-only)")

	// local-dist (MinIO) staging reclaim (#93). The orchestrator deletes its
	// own staging on success; this sweeps what's left by failed / cancelled /
	// crashed jobs, which otherwise grew the master's MinIO without bound.
	distStageInterval := flag.Duration("dist-staging-interval", 30*time.Minute,
		"MinIO staging sweep interval; 0 disables")
	distStageMaxAge := flag.Duration("dist-staging-max-age",
		time.Duration(intEnv("DIST_STAGING_MAX_AGE_H", 24))*time.Hour,
		"reclaim job staging idle longer than this (also the failed-job debugging window)")
	distStageLifecycleDays := flag.Int("dist-staging-lifecycle-days",
		intEnv("DIST_STAGING_LIFECYCLE_DAYS", 3),
		"MinIO bucket expiry for jobs/, in days; 0 disables")

	// $TMP_DIR/<job_id>/ reclaim (#207) — the local-disk twin of the MinIO sweep
	// above. run's finalize path clears the directory on every terminal outcome;
	// this collects what a killed or crashed server left behind, which nothing
	// else can see once the jobs/<id>.json state file is gone with it.
	tmpStageInterval := flag.Duration("tmp-staging-interval", 30*time.Minute,
		"TMP_DIR job-staging sweep interval; 0 disables")
	tmpStageMaxAge := flag.Duration("tmp-staging-max-age",
		time.Duration(intEnv("TMP_STAGING_MAX_AGE_H", 24))*time.Hour,
		"reclaim job staging idle longer than this (also the crashed-job debugging window)")
	flag.Parse()

	// Buffered so a job start/finalize never blocks on nudging the awswatch
	// inventory refresh; a pending nudge coalesces with any already queued.
	warmTrigger := make(chan struct{}, 1)
	mgr := encode.NewManager(encode.ManagerConfig{
		SourceDir:       *sourceDir,
		OutputDir:       *outputDir,
		TmpDir:          *tmpDir,
		ScriptsDir:      *scriptsDir,
		DockerImage:     *dockerImage,
		HostSourceDir:   *hostSourceDir,
		HostOutputDir:   *hostOutputDir,
		HostTmpDir:      *hostTmpDir,
		HostAWSDir:      *hostAWSDir,
		EncoderImage:    *encoderImage,
		StateMachineArn: *stateMachineArn,
		MaxConcurrent:   *maxConcurrent,
		InventoryNudge: func() {
			select {
			case warmTrigger <- struct{}{}:
			default:
			}
		},
	})
	mgr.Reconcile()

	// The -auto-watch flag / AUTO_WATCH env is only the *default*; a persisted
	// UI toggle (settings.json) wins so it sticks across restarts.
	mgr.InitSettings(*autoWatch)

	// Always run the watcher goroutine; it self-gates on the persisted
	// WatcherEnabled setting, which the UI toggles live.
	{
		defaults := encode.JobConfig{
			Codec:  *defaultCodec,
			Ladder: *defaultLadder,
			MaxRes: *defaultMaxRes,
			MinRes: *defaultMinRes,
			Target: encode.NormalizeTarget(encode.Target(*defaultTarget)),
		}
		w := watcher.New(*sourceDir, *watchInterval, mgr, defaults)
		go w.Run()
		log.Printf("watcher: monitoring %s every %s (enabled=%v, target=%s codec=%s)",
			*sourceDir, *watchInterval, mgr.WatcherEnabled(), *defaultTarget, *defaultCodec)
	}

	go awswatch.Run(context.Background(), awswatch.Config{
		Interval:            *awsWatchInterval,
		MaxLifetime:         *awsMaxLifetime,
		AutoTerminateStale:  *awsAutoTerminate,
		FailedStagingMaxAge: 1 * time.Hour,
		// Enforced on its own clock, not the 60s inventory tick: the retention
		// window above is measured in hours, and each pass costs a LIST plus a
		// head_object per job prefix.
		FailedStagingInterval: 30 * time.Minute,
		// Reclaim telemetry/state channels stranded by killed orchestrators
		// without waiting for the next cloud submit (#191). Hourly because the
		// sweep refuses to touch a queue younger than its 1h message retention,
		// so anything faster is SQS request spend on a set that cannot have
		// changed.
		TelemetryGCInterval: 1 * time.Hour,
		// Refresh the inventory immediately on a job start/finalize instead of
		// on the next tick, so the fleet panel and machine timeline keep up.
		Trigger: warmTrigger,
	})

	go diststage.Run(context.Background(), diststage.Config{
		Interval:      *distStageInterval,
		MaxAge:        *distStageMaxAge,
		LifecycleDays: *distStageLifecycleDays,
		// Never reclaim staging a queued/running job is still using.
		ActivePrefixes: mgr.ActiveDistPrefixes,
	})

	// Started after Reconcile, so a job resumed from a state file is already in
	// the keep-list before the first sweep looks at its directory.
	go tmpstage.Run(context.Background(), tmpstage.Config{
		Dir:       *tmpDir,
		Interval:  *tmpStageInterval,
		MaxAge:    *tmpStageMaxAge,
		ActiveIDs: mgr.ActiveJobIDs,
	})

	srv := api.NewServer(mgr)
	srv.Version = version
	srv.GitSha = gitSha
	srv.ImageTag = imageTag
	srv.CloudImage = *dockerImage
	srv.GHCRImage = *ghcrImage
	srv.DevMount = os.Getenv("DEV_MOUNT") == "1"

	log.Printf("encoder server %s (%s, image %s) listening on %s", version, gitSha, imageTag, *addr)
	log.Printf("  source: %s", *sourceDir)
	log.Printf("  output: %s", *outputDir)
	log.Printf("  tmp: %s", *tmpDir)
	log.Printf("  scripts: %s", *scriptsDir)
	log.Printf("  max concurrent: %d", *maxConcurrent)
	if err := http.ListenAndServe(*addr, srv.Mux); err != nil {
		log.Fatal(err)
	}
}
