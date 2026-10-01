{
  "name": "sample-legs",
  "application": "demo-app",
  "triggers": [],
  "stages": [
    {
      "refId": "1",
      "type": "runJobManifest",
      "name": "Prepare",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [],
      "manifest": {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
          "name": "prepare"
        },
        "spec": {
          "backoffLimit": 0,
          "template": {
            "spec": {
              "restartPolicy": "Never",
              "containers": [
                {
                  "name": "runner",
                  "image": "registry.example.com/tools/runner:1.0",
                  "command": [
                    "/bin/sh",
                    "-c",
                    "echo prepare\n"
                  ],
                  "env": []
                }
              ]
            }
          }
        }
      }
    },
    {
      "refId": "qa",
      "type": "deployManifest",
      "name": "Deploy qa leg",
      "account": "demo-account",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "1"
      ],
      "stageEnabled": {
        "type": "expression",
        "expression": "${parameters.target == 'qa'}"
      },
      "manifests": [
        {
          "apiVersion": "batch/v1",
          "kind": "CronJob",
          "metadata": {
            "name": "enqueuer-qa"
          },
          "spec": {
            "schedule": "*/15 * * * *",
            "jobTemplate": {
              "spec": {
                "template": {
                  "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                      {
                        "name": "enqueuer",
                        "image": "registry.example.com/tools/enqueuer:2.1",
                        "command": [
                          "/bin/sh",
                          "-c",
                          "echo enqueue\n"
                        ],
                        "env": [
                          {
                            "name": "TARGET",
                            "value": "qa"
                          },
                          {
                            "name": "BATCH",
                            "value": "50"
                          },
                          {
                            "name": "REGION",
                            "value": "us-east-1"
                          },
                          {
                            "name": "TIMEOUT",
                            "value": "30"
                          },
                          {
                            "name": "RETRIES",
                            "value": "3"
                          },
                          {
                            "name": "LOG_LEVEL",
                            "value": "info"
                          },
                          {
                            "name": "QUEUE",
                            "value": "jobs"
                          }
                        ]
                      }
                    ]
                  }
                }
              }
            }
          }
        }
      ]
    },
    {
      "refId": "stage",
      "type": "deployManifest",
      "name": "Deploy stage leg",
      "account": "demo-account",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "1"
      ],
      "stageEnabled": {
        "type": "expression",
        "expression": "${parameters.target == 'stage'}"
      },
      "manifests": [
        {
          "apiVersion": "batch/v1",
          "kind": "CronJob",
          "metadata": {
            "name": "enqueuer-stage"
          },
          "spec": {
            "schedule": "*/15 * * * *",
            "jobTemplate": {
              "spec": {
                "template": {
                  "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                      {
                        "name": "enqueuer",
                        "image": "registry.example.com/tools/enqueuer:2.1",
                        "command": [
                          "/bin/sh",
                          "-c",
                          "echo enqueue\n"
                        ],
                        "env": [
                          {
                            "name": "TARGET",
                            "value": "stage"
                          },
                          {
                            "name": "BATCH",
                            "value": "50"
                          },
                          {
                            "name": "REGION",
                            "value": "us-east-1"
                          },
                          {
                            "name": "TIMEOUT",
                            "value": "30"
                          },
                          {
                            "name": "RETRIES",
                            "value": "3"
                          },
                          {
                            "name": "LOG_LEVEL",
                            "value": "info"
                          },
                          {
                            "name": "QUEUE",
                            "value": "jobs"
                          }
                        ]
                      }
                    ]
                  }
                }
              }
            }
          }
        }
      ]
    },
    {
      "refId": "prod",
      "type": "deployManifest",
      "name": "Deploy prod leg",
      "account": "demo-account",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "1"
      ],
      "stageEnabled": {
        "type": "expression",
        "expression": "${parameters.target == 'prod'}"
      },
      "manifests": [
        {
          "apiVersion": "batch/v1",
          "kind": "CronJob",
          "metadata": {
            "name": "enqueuer-prod"
          },
          "spec": {
            "schedule": "*/15 * * * *",
            "jobTemplate": {
              "spec": {
                "template": {
                  "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                      {
                        "name": "enqueuer",
                        "image": "registry.example.com/tools/enqueuer:2.1",
                        "command": [
                          "/bin/sh",
                          "-c",
                          "echo enqueue\n"
                        ],
                        "env": [
                          {
                            "name": "TARGET",
                            "value": "prod"
                          },
                          {
                            "name": "BATCH",
                            "value": "50"
                          },
                          {
                            "name": "REGION",
                            "value": "us-east-1"
                          },
                          {
                            "name": "TIMEOUT",
                            "value": "30"
                          },
                          {
                            "name": "RETRIES",
                            "value": "3"
                          },
                          {
                            "name": "LOG_LEVEL",
                            "value": "info"
                          },
                          {
                            "name": "QUEUE",
                            "value": "jobs"
                          }
                        ]
                      }
                    ]
                  }
                }
              }
            }
          }
        }
      ]
    }
  ]
}
