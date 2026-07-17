package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/jonathaneoliver/encoder/internal/api"
	"github.com/jonathaneoliver/encoder/internal/awswatch"
	"github.com/jonathaneoliver/encoder/internal/encode"
	"github.com/jonathaneoliver/encoder/internal/watcher"
)

// Version + gitSha are stamped at build time via
// `-ldflags "-X main.version=... -X main.gitSha=..."`. The Makefile
// reads ./VERSION and `git rev-parse --short HEAD`; unstamped builds
// (plain `go build`) fall back to the defaults below.
var (
	version = "dev"
	gitSha  = "unknown"
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
	dockerImage := flag.String("docker-image", env("DOCKER_IMAGE", "ghcr.io/jonathaneoliver/encoder:latest"), "Docker image used for remote (EC2) encodes")
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
	defaultTarget := flag.String("default-target", env("DEFAULT_TARGET", "local"), "default encode target: cloud or local")
	defaultCodec := flag.String("default-codec", env("DEFAULT_CODEC", "both"), "default codec: h264, hevc, both")
	defaultLadder := flag.String("default-ladder", env("DEFAULT_LADDER", "apple-uniq-live"), "default encoding ladder profile")
	defaultMaxRes := flag.String("default-max-res", env("DEFAULT_MAX_RES", ""), "default max resolution (empty = no limit)")
	maxConcurrent := flag.Int("max-concurrent", intEnv("MAX_CONCURRENT", 1), "max concurrent encode jobs")
	// AWS watchdog (issue #5 phase 5). Polls `encoder.cloud.inventory` on
	// an interval; any instance older than the lifetime budget is flagged
	// as a leak and, when AUTO_TERMINATE_STALE=true, force-terminated.
	awsWatchInterval := flag.Duration("aws-watch-interval", 60*time.Second, "AWS inventory poll interval; 0 disables")
	awsMaxLifetime := flag.Duration("aws-max-lifetime", 4*time.Hour, "terminate EC2 instances older than this")
	awsAutoTerminate := flag.Bool("aws-auto-terminate-stale",
		env("AUTO_TERMINATE_STALE", "true") == "true",
		"force-terminate stale instances (false = warn-only)")
	flag.Parse()

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
	})
	mgr.Reconcile()

	if *autoWatch {
		defaults := encode.JobConfig{
			Codec:  *defaultCodec,
			Ladder: *defaultLadder,
			MaxRes: *defaultMaxRes,
			Target: encode.Target(*defaultTarget),
		}
		w := watcher.New(*sourceDir, *watchInterval, mgr, defaults)
		go w.Run()
		log.Printf("watcher: monitoring %s every %s (target=%s codec=%s)",
			*sourceDir, *watchInterval, *defaultTarget, *defaultCodec)
	}

	go awswatch.Run(context.Background(), awswatch.Config{
		Interval:            *awsWatchInterval,
		MaxLifetime:         *awsMaxLifetime,
		AutoTerminateStale:  *awsAutoTerminate,
		FailedStagingMaxAge: 1 * time.Hour,
		// Keep one small box warm during active cloud-batch runs so the
		// packaging tail doesn't cold-start; 0 disables (min_vcpus stays 0).
		WarmMinVCPUs: intEnv("WARM_MIN_VCPUS", 2),
	})

	srv := api.NewServer(mgr)
	srv.Version = version
	srv.GitSha = gitSha
	srv.CloudImage = *dockerImage

	log.Printf("encoder server %s (%s) listening on %s", version, gitSha, *addr)
	log.Printf("  source: %s", *sourceDir)
	log.Printf("  output: %s", *outputDir)
	log.Printf("  tmp: %s", *tmpDir)
	log.Printf("  scripts: %s", *scriptsDir)
	log.Printf("  max concurrent: %d", *maxConcurrent)
	if err := http.ListenAndServe(*addr, srv.Mux); err != nil {
		log.Fatal(err)
	}
}
