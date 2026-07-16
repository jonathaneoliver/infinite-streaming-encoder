{
  "Comment": "Encoder pipeline: mezzanine -> fan-out variants + audio -> per-codec package/HLS/byteranges.",
  "StartAt": "Mezzanine",
  "States": {

    "Mezzanine": {
      "Type": "Task",
      "Resource": "arn:aws:states:::batch:submitJob.sync",
      "Parameters": {
        "JobName.$": "States.Format('mezz-{}', $$.Execution.Name)",
        "JobQueue": "${job_queue_arn}",
        "JobDefinition": "${mezzanine_def}",
        "Parameters": {
          "s3_in.$": "$.s3_input",
          "s3_out.$": "$.s3_prefix"
        },
        "ContainerOverrides": {
          "Environment": [
            { "Name": "JOB_ID", "Value.$": "$$.Execution.Name" }
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
      "Comment": "Audio + variants run in parallel; both must succeed before packaging.",
      "Type": "Parallel",
      "ResultPath": "$.fanout",
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
                "Parameters": {
                  "s3_mezz.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
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
              "Comment": "Fan out across (codec, tier); each variant fans out again across chunks, then concats. 12 variants x N chunks. Batch queue depth caps real parallelism.",
              "Type": "Map",
              "ItemsPath": "$.variants",
              "MaxConcurrency": 6,
              "ItemSelector": {
                "codec.$": "$$.Map.Item.Value.codec",
                "tier.$": "$$.Map.Item.Value.tier",
                "vcpu.$": "$$.Map.Item.Value.vcpu",
                "memory.$": "$$.Map.Item.Value.memory",
                "s3_prefix.$": "$.s3_prefix",
                "two_pass.$": "$.two_pass",
                "chunk_indices.$": "$.chunk_indices",
                "chunk_duration.$": "$.chunk_duration"
              },
              "ItemProcessor": {
                "StartAt": "EncodeChunks",
                "States": {
                  "EncodeChunks": {
                    "Comment": "One encode job per chunk index, concurrent across chunks. A single-chunk clip runs one job (chunk 0 = whole clip).",
                    "Type": "Map",
                    "ItemsPath": "$.chunk_indices",
                    "ItemSelector": {
                      "codec.$": "$.codec",
                      "tier.$": "$.tier",
                      "vcpu.$": "$.vcpu",
                      "memory.$": "$.memory",
                      "s3_prefix.$": "$.s3_prefix",
                      "two_pass.$": "$.two_pass",
                      "chunk_index.$": "$$.Map.Item.Value",
                      "chunk_duration.$": "$.chunk_duration"
                    },
                    "ItemProcessor": {
                      "StartAt": "EncodeChunk",
                      "States": {
                        "EncodeChunk": {
                          "Type": "Task",
                          "Resource": "arn:aws:states:::batch:submitJob.sync",
                          "Parameters": {
                            "JobName.$": "States.Format('var-{}-{}-c{}-{}', $.codec, $.tier, $.chunk_index, $$.Execution.Name)",
                            "JobQueue": "${job_queue_arn}",
                            "JobDefinition": "${variant_def}",
                            "Parameters": {
                              "codec.$": "$.codec",
                              "tier.$": "$.tier",
                              "chunk_index.$": "States.Format('{}', $.chunk_index)",
                              "s3_mezz.$": "$.s3_prefix",
                              "s3_out.$": "$.s3_prefix"
                            },
                            "ContainerOverrides": {
                              "Environment": [
                                { "Name": "TWO_PASS", "Value.$": "$.two_pass" },
                                { "Name": "CHUNK_DURATION_S", "Value.$": "$.chunk_duration" }
                              ],
                              "ResourceRequirements": [
                                { "Type": "VCPU", "Value.$": "$.vcpu" },
                                { "Type": "MEMORY", "Value.$": "$.memory" }
                              ]
                            }
                          },
                          "End": true
                        }
                      }
                    },
                    "ResultPath": "$.chunk_results",
                    "Next": "ConcatVariant"
                  },
                  "ConcatVariant": {
                    "Comment": "Join the variant's chunk encodes into the whole variant (stream copy).",
                    "Type": "Task",
                    "Resource": "arn:aws:states:::batch:submitJob.sync",
                    "Parameters": {
                      "JobName.$": "States.Format('concat-{}-{}-{}', $.codec, $.tier, $$.Execution.Name)",
                      "JobQueue": "${job_queue_arn}",
                      "JobDefinition": "${concat_def}",
                      "Parameters": {
                        "codec.$": "$.codec",
                        "tier.$": "$.tier",
                        "s3_mezz.$": "$.s3_prefix",
                        "s3_chunks.$": "$.s3_prefix",
                        "s3_out.$": "$.s3_prefix"
                      },
                      "ContainerOverrides": {
                        "Environment": [
                          { "Name": "CHUNK_DURATION_S", "Value.$": "$.chunk_duration" }
                        ]
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
                { "Variable": "$.do_h264", "BooleanEquals": true, "Next": "PackageH264" }
              ],
              "Default": "SkipH264"
            },
            "SkipH264": { "Type": "Succeed" },
            "PackageH264": {
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('pkg-h264-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${package_def}",
                "Parameters": {
                  "codec": "h264",
                  "s3_variants.$": "$.s3_prefix",
                  "s3_audio.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
                }
              },
              "ResultPath": null,
              "Next": "HlsH264"
            },
            "HlsH264": {
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('hls-h264-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${hls_def}",
                "Parameters": {
                  "codec": "h264",
                  "s3_package.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
                }
              },
              "ResultPath": null,
              "Next": "ByterangesH264"
            },
            "ByterangesH264": {
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('br-h264-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${byteranges_def}",
                "Parameters": {
                  "codec": "h264",
                  "s3_package.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
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
                { "Variable": "$.do_hevc", "BooleanEquals": true, "Next": "PackageHevc" }
              ],
              "Default": "SkipHevc"
            },
            "SkipHevc": { "Type": "Succeed" },
            "PackageHevc": {
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('pkg-hevc-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${package_def}",
                "Parameters": {
                  "codec": "hevc",
                  "s3_variants.$": "$.s3_prefix",
                  "s3_audio.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
                }
              },
              "ResultPath": null,
              "Next": "HlsHevc"
            },
            "HlsHevc": {
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('hls-hevc-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${hls_def}",
                "Parameters": {
                  "codec": "hevc",
                  "s3_package.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
                }
              },
              "ResultPath": null,
              "Next": "ByterangesHevc"
            },
            "ByterangesHevc": {
              "Type": "Task",
              "Resource": "arn:aws:states:::batch:submitJob.sync",
              "Parameters": {
                "JobName.$": "States.Format('br-hevc-{}', $$.Execution.Name)",
                "JobQueue": "${job_queue_arn}",
                "JobDefinition": "${byteranges_def}",
                "Parameters": {
                  "codec": "hevc",
                  "s3_package.$": "$.s3_prefix",
                  "s3_out.$": "$.s3_prefix"
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
      "Cause": "See CloudWatch Logs under /aws/batch/encoder for the failing job's stream."
    }

  }
}
