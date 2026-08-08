{
  "Comment": "Encoder pipeline: mezzanine -> fan-out variants + audio -> per-codec package/HLS/byteranges.",
  "StartAt": "MezzCheck",
  "States": {

    "MezzCheck": {
      "Comment": "Skip the mezzanine job entirely when a prior job of the same source already produced it (mezz_cached from buildSFNInput) — variants + audio read it straight from the source-keyed cache (s3_mezz).",
      "Type": "Choice",
      "Choices": [
        { "Variable": "$.mezz_cached", "BooleanEquals": true, "Next": "FanOut" }
      ],
      "Default": "Mezzanine"
    },

    "Mezzanine": {
      "Type": "Task",
      "Resource": "arn:aws:states:::batch:submitJob.sync",
      "Parameters": {
        "JobName.$": "States.Format('mezz-{}', $$.Execution.Name)",
        "JobQueue": "${job_queue_arn}",
        "JobDefinition": "${mezzanine_def}",
        "ShareIdentifier": "encode",
        "SchedulingPriorityOverride.$": "$.prio_mezz",
        "Parameters": {
          "s3_in.$": "$.s3_input",
          "s3_out.$": "$.s3_mezz"
        },
        "ContainerOverrides": {
          "Environment": [
            { "Name": "ENCODER_TELEMETRY_EXEC", "Value.$": "$$.Execution.Name" },
            { "Name": "JOB_ID", "Value.$": "$$.Execution.Name" },
            { "Comment": "Duration limit (#184), applied by truncating the mezzanine — every variant, chunk and the audio are cut from it, so this is the only state that needs it. buildSFNInput ALWAYS supplies time_limit (\"0\" when unset): Value.$ on a key the input omits fails the state at runtime.",
              "Name": "TIME_LIMIT_S", "Value.$": "$.time_limit" }
          ]
        }
      },
      "ResultPath": "$.mezzanine",
      "Retry": [
        {
          "Comment": "Retry only transient orchestration errors (submit throttling / Batch service blips). A States.TaskFailed here means the Batch job itself failed — its evaluate_on_exit already decided recoverability (retry spot reclaims, EXIT on everything else), so re-running the whole job just repeats an unrecoverable failure. Let it fall straight through to Catch -> Failed.",
          "ErrorEquals": ["Batch.AWSBatchException", "States.Timeout"],
          "IntervalSeconds": 15,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "Next": "Failed"
        }
      ],
      "Next": "FanOut"
    },

    "FanOut": {
      "Comment": "Audio + variants run in parallel; both must succeed before packaging. ResultPath is null: the encode/audio results (Batch job JSON per chunk) aren't needed downstream — package-all reads everything from S3 — and carrying an array of every chunk job's result would blow the 256 KB state-data limit at PerCodec.",
      "Type": "Parallel",
      "ResultPath": null,
      "Branches": [

        {
          "StartAt": "Audio",
          "States": {
            "Audio": {
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('audio-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${audio_def}",
                "ShareIdentifier": "encode",
                "SchedulingPriorityOverride.$": "$.prio_audio",
                "Parameters": {
                  "s3_mezz.$": "$.s3_mezz",
                  "s3_out.$": "$.s3_prefix"
                },
                "ContainerOverrides": {
                  "Environment": [
                    { "Name": "ENCODER_TELEMETRY_EXEC", "Value.$": "$$.Execution.Name" }
                  ]
                }
              },
              "End": true
            }
          }
        },

        {
          "StartAt": "Variants",
          "States": {
            "Variants": {
              "Comment": "Fan out across (codec, tier); each variant fans out again across chunks. The chunks are joined inline by the package-all phase (no separate concat job). MaxConcurrency 40 covers any ladder (3 codecs x ~12 rungs) so ALL variants of a file submit at once — real parallelism is still capped by compute-env max_vcpus (the rest sit RUNNABLE, ordered by schedulingPriority). Atomic fan-out is what makes the app-side launch gate correct: an earlier job has submitted every one of its jobs before the next job's execution starts.",
              "Type": "Map",
              "ItemsPath": "$.variants",
              "MaxConcurrency": 40,
              "ItemSelector": {
                "codec.$": "$$.Map.Item.Value.codec",
                "label.$": "$$.Map.Item.Value.label",
                "width.$": "$$.Map.Item.Value.width",
                "height.$": "$$.Map.Item.Value.height",
                "bitrate.$": "$$.Map.Item.Value.bitrate",
                "preset.$": "$$.Map.Item.Value.preset",
                "vcpu.$": "$$.Map.Item.Value.vcpu",
                "memory.$": "$$.Map.Item.Value.memory",
                "priority.$": "$$.Map.Item.Value.priority",
                "s3_prefix.$": "$.s3_prefix",
                "s3_mezz.$": "$.s3_mezz",
                "two_pass.$": "$$.Map.Item.Value.two_pass",
                "extra_args.$": "$$.Map.Item.Value.extra_args",
                "chunks.$": "$$.Map.Item.Value.chunks",
                "chunk_duration.$": "$$.Map.Item.Value.chunk_duration",
                "maxrate_percent.$": "$.maxrate_percent",
                "bufsize_multiplier.$": "$.bufsize_multiplier",
                "segment_duration.$": "$.segment_duration",
                "partial_duration.$": "$.partial_duration",
                "gop_duration.$": "$.gop_duration",
                "time_limit.$": "$.time_limit",
                "burnin.$": "$.burnin",
                "est_vmaf.$": "$$.Map.Item.Value.est_vmaf",
                "est_vmaf_clamped.$": "$$.Map.Item.Value.est_vmaf_clamped",
                "chunked.$": "$$.Map.Item.Value.chunked",
                "content_duration.$": "$$.Map.Item.Value.content_duration"
              },
              "ItemProcessor": {
                "StartAt": "Chunked",
                "States": {
                  "Chunked": {
                    "Comment": "Single-chunk (whole-variant) runs skip the chunk fan-out + concat entirely and encode the whole variant in one job (chunk_index=-1 writes the un-suffixed file directly).",
                    "Type": "Choice",
                    "Choices": [
                      { "Variable": "$.chunked", "StringEquals": "true", "Next": "EncodeChunks" }
                    ],
                    "Default": "EncodeWhole"
                  },
                  "EncodeWhole": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::batch:submitJob.sync",
                    "Parameters": {
                      "JobName.$": "States.Format('var-{}-{}-whole-{}', $.codec, $.label, $$.Execution.Name)",
                      "JobQueue": "${job_queue_arn}",
                      "JobDefinition": "${variant_def}",
                      "ShareIdentifier": "encode",
                      "SchedulingPriorityOverride.$": "$.priority",
                      "Parameters": {
                        "codec.$": "$.codec",
                        "label.$": "$.label",
                        "width.$": "$.width",
                        "height.$": "$.height",
                        "bitrate.$": "$.bitrate",
                        "preset.$": "$.preset",
                        "chunk_index": "-1",
                        "chunk_start": "-1",
                        "chunk_span": "-1",
                        "content_duration.$": "$.content_duration",
                        "s3_mezz.$": "$.s3_mezz",
                        "s3_out.$": "$.s3_prefix"
                      },
                      "ContainerOverrides": {
                        "Environment": [
                          { "Name": "ENCODER_TELEMETRY_EXEC", "Value.$": "$$.Execution.Name" },
                          { "Name": "TWO_PASS", "Value.$": "$.two_pass" },
                          { "Name": "EXTRA_ARGS", "Value.$": "$.extra_args" },
                          { "Name": "CHUNK_DURATION_S", "Value.$": "$.chunk_duration" },
                          { "Name": "MAXRATE_PERCENT", "Value.$": "$.maxrate_percent" },
                          { "Name": "BUFSIZE_MULT", "Value.$": "$.bufsize_multiplier" },
                          { "Name": "SEGMENT_DURATION", "Value.$": "$.segment_duration" },
                          { "Name": "GOP_DURATION", "Value.$": "$.gop_duration" },
                          { "Comment": "Not applied here — the mezzanine is already truncated. This tells the plan-vs-media check that the chunk plan is a deliberate PREFIX of the mezzanine, because -t on a stream copy overshoots the limit by a frame or two (#184).",
                            "Name": "TIME_LIMIT_S", "Value.$": "$.time_limit" },
                          { "Name": "BURNIN", "Value.$": "$.burnin" },
                          { "Name": "EST_VMAF", "Value.$": "$.est_vmaf" },
                          { "Name": "EST_VMAF_CLAMPED", "Value.$": "$.est_vmaf_clamped" },
                          { "Name": "ENCODE_THREADS", "Value": "2" }
                        ],
                        "ResourceRequirements": [
                          { "Type": "VCPU", "Value.$": "$.vcpu" },
                          { "Type": "MEMORY", "Value.$": "$.memory" }
                        ]
                      }
                    },
                    "ResultPath": null,
                    "End": true
                  },
                  "EncodeChunks": {
                    "Comment": "One encode job per chunk index, concurrent across chunks. A single-chunk clip runs one job (chunk 0 = whole clip).",
                    "Type": "Map",
                    "ItemsPath": "$.chunks",
                    "ItemSelector": {
                      "codec.$": "$.codec",
                      "label.$": "$.label",
                      "width.$": "$.width",
                      "height.$": "$.height",
                      "bitrate.$": "$.bitrate",
                      "preset.$": "$.preset",
                      "vcpu.$": "$.vcpu",
                      "memory.$": "$.memory",
                      "priority.$": "$.priority",
                      "s3_prefix.$": "$.s3_prefix",
                      "s3_mezz.$": "$.s3_mezz",
                      "two_pass.$": "$.two_pass",
                      "extra_args.$": "$.extra_args",
                      "chunk_index.$": "$$.Map.Item.Value.index",
                      "chunk_start.$": "$$.Map.Item.Value.start_s",
                      "chunk_span.$": "$$.Map.Item.Value.duration_s",
                      "content_duration.$": "$.content_duration",
                      "chunk_duration.$": "$.chunk_duration",
                      "maxrate_percent.$": "$.maxrate_percent",
                      "bufsize_multiplier.$": "$.bufsize_multiplier",
                      "segment_duration.$": "$.segment_duration",
                      "gop_duration.$": "$.gop_duration",
                      "time_limit.$": "$.time_limit",
                      "burnin.$": "$.burnin",
                      "est_vmaf.$": "$.est_vmaf",
                      "est_vmaf_clamped.$": "$.est_vmaf_clamped"
                    },
                    "ItemProcessor": {
                      "StartAt": "EncodeChunk",
                      "States": {
                        "EncodeChunk": {
                          "Type": "Task",
                          "Resource": "arn:aws:states:::batch:submitJob.sync",
                          "Parameters": {
                            "JobName.$": "States.Format('var-{}-{}-c{}-{}', $.codec, $.label, $.chunk_index, $$.Execution.Name)",
                            "JobQueue": "${job_queue_arn}",
                            "JobDefinition": "${variant_def}",
                            "ShareIdentifier": "encode",
                            "SchedulingPriorityOverride.$": "$.priority",
                            "Parameters": {
                              "codec.$": "$.codec",
                              "label.$": "$.label",
                              "width.$": "$.width",
                              "height.$": "$.height",
                              "bitrate.$": "$.bitrate",
                              "preset.$": "$.preset",
                              "chunk_index.$": "States.Format('{}', $.chunk_index)",
                              "chunk_start.$": "$.chunk_start",
                              "chunk_span.$": "$.chunk_span",
                              "content_duration.$": "$.content_duration",
                              "s3_mezz.$": "$.s3_mezz",
                              "s3_out.$": "$.s3_prefix"
                            },
                            "ContainerOverrides": {
                              "Environment": [
                                { "Name": "ENCODER_TELEMETRY_EXEC", "Value.$": "$$.Execution.Name" },
                                { "Name": "TWO_PASS", "Value.$": "$.two_pass" },
                                { "Name": "EXTRA_ARGS", "Value.$": "$.extra_args" },
                                { "Name": "CHUNK_DURATION_S", "Value.$": "$.chunk_duration" },
                                { "Name": "MAXRATE_PERCENT", "Value.$": "$.maxrate_percent" },
                                { "Name": "BUFSIZE_MULT", "Value.$": "$.bufsize_multiplier" },
                                { "Name": "SEGMENT_DURATION", "Value.$": "$.segment_duration" },
                                { "Name": "GOP_DURATION", "Value.$": "$.gop_duration" },
                                { "Comment": "Not applied here — the mezzanine is already truncated. This tells the plan-vs-media check that the chunk plan is a deliberate PREFIX of the mezzanine, because -t on a stream copy overshoots the limit by a frame or two (#184).",
                                  "Name": "TIME_LIMIT_S", "Value.$": "$.time_limit" },
                                { "Name": "BURNIN", "Value.$": "$.burnin" },
                                { "Name": "EST_VMAF", "Value.$": "$.est_vmaf" },
                                { "Name": "EST_VMAF_CLAMPED", "Value.$": "$.est_vmaf_clamped" },
                                { "Name": "ENCODE_THREADS", "Value": "2" }
                              ],
                              "ResourceRequirements": [
                                { "Type": "VCPU", "Value.$": "$.vcpu" },
                                { "Type": "MEMORY", "Value.$": "$.memory" }
                              ]
                            }
                          },
                          "ResultPath": null,
                          "End": true
                        }
                      }
                    },
                    "End": true
                  }
                }
              },
              "End": true
            }
          }
        }

      ],
      "Next": "PerCodec"
    },

    "PerCodec": {
      "Comment": "Parallel per-codec packaging chain. Each branch: Package -> HLS -> Byteranges.",
      "Type": "Parallel",
      "ResultPath": "$.per_codec",
      "Branches": [

        {
          "StartAt": "H264Selected",
          "States": {
            "H264Selected": {
              "Comment": "Only package h264 if it was actually encoded (do_h264 from buildSFNInput). A single-codec job would otherwise fail here with 'no h264 variants found'.",
              "Type": "Choice",
              "Choices": [
                { "Variable": "$.do_h264", "BooleanEquals": true, "Next": "PackageAllH264" }
              ],
              "Default": "SkipH264"
            },
            "SkipH264": { "Type": "Succeed" },
            "PackageAllH264": {
              "Comment": "Combined package + byteranges + fMP4 HLS in one job (downloads the ladder once).",
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('pkgall-h264-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${package_all_def}",
                "ShareIdentifier": "encode",
                "SchedulingPriorityOverride.$": "$.prio_pkg",
                "Parameters": {
                  "codec": "h264",
                  "s3_variants.$": "$.s3_prefix",
                  "s3_audio.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
                },
                "ContainerOverrides": {
                  "Environment": [
                    { "Name": "ENCODER_TELEMETRY_EXEC", "Value.$": "$$.Execution.Name" },
                    { "Name": "SEGMENT_DURATION", "Value.$": "$.segment_duration" },
                    { "Name": "PARTIAL_DURATION", "Value.$": "$.partial_duration" }
                  ]
                }
              },
              "End": true
            }
          }
        },

        {
          "StartAt": "HevcSelected",
          "States": {
            "HevcSelected": {
              "Comment": "Only package hevc if it was actually encoded (do_hevc from buildSFNInput). A single-codec job would otherwise fail here with 'no hevc variants found'.",
              "Type": "Choice",
              "Choices": [
                { "Variable": "$.do_hevc", "BooleanEquals": true, "Next": "PackageAllHevc" }
              ],
              "Default": "SkipHevc"
            },
            "SkipHevc": { "Type": "Succeed" },
            "PackageAllHevc": {
              "Comment": "Combined package + byteranges + fMP4 HLS in one job (downloads the ladder once).",
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('pkgall-hevc-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${package_all_def}",
                "ShareIdentifier": "encode",
                "SchedulingPriorityOverride.$": "$.prio_pkg",
                "Parameters": {
                  "codec": "hevc",
                  "s3_variants.$": "$.s3_prefix",
                  "s3_audio.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
                },
                "ContainerOverrides": {
                  "Environment": [
                    { "Name": "ENCODER_TELEMETRY_EXEC", "Value.$": "$$.Execution.Name" },
                    { "Name": "SEGMENT_DURATION", "Value.$": "$.segment_duration" },
                    { "Name": "PARTIAL_DURATION", "Value.$": "$.partial_duration" }
                  ]
                }
              },
              "End": true
            }
          }
        },

        {
          "StartAt": "Av1Selected",
          "States": {
            "Av1Selected": {
              "Comment": "Only package av1 if it was actually encoded (do_av1 from buildSFNInput). A single-codec job would otherwise fail here with 'no av1 variants found'.",
              "Type": "Choice",
              "Choices": [
                { "Variable": "$.do_av1", "BooleanEquals": true, "Next": "PackageAllAv1" }
              ],
              "Default": "SkipAv1"
            },
            "SkipAv1": { "Type": "Succeed" },
            "PackageAllAv1": {
              "Comment": "Combined package + byteranges + fMP4 HLS in one job (downloads the ladder once).",
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('pkgall-av1-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${package_all_def}",
                "ShareIdentifier": "encode",
                "SchedulingPriorityOverride.$": "$.prio_pkg",
                "Parameters": {
                  "codec": "av1",
                  "s3_variants.$": "$.s3_prefix",
                  "s3_audio.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
                },
                "ContainerOverrides": {
                  "Environment": [
                    { "Name": "ENCODER_TELEMETRY_EXEC", "Value.$": "$$.Execution.Name" },
                    { "Name": "SEGMENT_DURATION", "Value.$": "$.segment_duration" },
                    { "Name": "PARTIAL_DURATION", "Value.$": "$.partial_duration" }
                  ]
                }
              },
              "End": true
            }
          }
        }

      ],
      "Next": "Success"
    },

    "Success": {
      "Type": "Succeed"
    },

    "Failed": {
      "Type": "Fail",
      "Error": "EncodeFailed",
      "Cause": "See CloudWatch Logs under /aws/batch/infinite-streaming-encoder for the failing job's stream."
    }

  }
}
