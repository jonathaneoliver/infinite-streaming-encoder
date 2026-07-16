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
              "MaxConcurrency": 12,
              "ItemSelector": {
                "codec.$": "$$.Map.Item.Value.codec",
                "tier.$": "$$.Map.Item.Value.tier",
                "vcpu.$": "$$.Map.Item.Value.vcpu",
                "memory.$": "$$.Map.Item.Value.memory",
                "s3_prefix.$": "$.s3_prefix",
                "two_pass.$": "$.two_pass",
                "chunk_indices.$": "$.chunk_indices"
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
                      "chunk_index.$": "$$.Map.Item.Value"
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
                                { "Name": "TWO_PASS", "Value.$": "$.two_pass" }
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
          "StartAt": "PackageH264",
          "States": {
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
          "StartAt": "PackageHevc",
          "States": {
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
