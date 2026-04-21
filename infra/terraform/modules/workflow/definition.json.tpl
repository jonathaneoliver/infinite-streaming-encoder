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
          "ErrorEquals": ["States.TaskFailed"],
          "IntervalSeconds": 30,
          "MaxAttempts": 2,
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
              "Comment": "Fan out across (codec, tier). 12 variants for h264+hevc x 6 tiers. Batch queue depth caps real parallelism.",
              "Type": "Map",
              "ItemsPath": "$.variants",
              "MaxConcurrency": 12,
              "ItemProcessor": {
                "StartAt": "EncodeVariant",
                "States": {
                  "EncodeVariant": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::batch:submitJob.sync",
                    "Parameters": {
                      "JobName.$": "States.Format('var-{}-{}-{}', $.codec, $.tier, $$.Execution.Name)",
                      "JobQueue": "${job_queue_arn}",
                      "JobDefinition": "${variant_def}",
                      "Parameters": {
                        "codec.$": "$.codec",
                        "tier.$": "$.tier",
                        "s3_mezz.$": "$.s3_prefix",
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
