package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/jonathaneoliver/encoder/internal/api"
	"github.com/jonathaneoliver/encoder/internal/encode"
	"github.com/jonathaneoliver/encoder/internal/watcher"
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
	dockerImage := flag.String("docker-image", env("DOCKER_IMAGE", "ghcr.io/jonathaneoliver/infinite-streaming:latest"), "Docker image for local encoding")
	autoWatch := flag.Bool("auto-watch", env("AUTO_WATCH", "true") == "true", "auto-encode new files in source dir")
	watchInterval := flag.Duration("watch-interval", 30*time.Second, "filesystem watch polling interval")
	defaultTarget := flag.String("default-target", env("DEFAULT_TARGET", "local"), "default encode target: cloud or local")
	defaultCodec := flag.String("default-codec", env("DEFAULT_CODEC", "both"), "default codec: h264, hevc, both")
	defaultMaxRes := flag.String("default-max-res", env("DEFAULT_MAX_RES", ""), "default max resolution (empty = no limit)")
	maxConcurrent := flag.Int("max-concurrent", intEnv("MAX_CONCURRENT", 1), "max concurrent encode jobs")
	flag.Parse()

	mgr := encode.NewManager(*sourceDir, *outputDir, *tmpDir, *scriptsDir, *dockerImage, *maxConcurrent)

	if *autoWatch {
		defaults := encode.JobConfig{
			Codec:  *defaultCodec,
			MaxRes: *defaultMaxRes,
			Target: encode.Target(*defaultTarget),
		}
		w := watcher.New(*sourceDir, *watchInterval, mgr, defaults)
		go w.Run()
		log.Printf("watcher: monitoring %s every %s (target=%s codec=%s)",
			*sourceDir, *watchInterval, *defaultTarget, *defaultCodec)
	}

	srv := api.NewServer(mgr)

	log.Printf("encoder server listening on %s", *addr)
	log.Printf("  source: %s", *sourceDir)
	log.Printf("  output: %s", *outputDir)
	log.Printf("  tmp: %s", *tmpDir)
	log.Printf("  scripts: %s", *scriptsDir)
	log.Printf("  max concurrent: %d", *maxConcurrent)
	if err := http.ListenAndServe(*addr, srv.Mux); err != nil {
		log.Fatal(err)
	}
}
