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
      "refId": "old-a",
      "type": "deployManifest",
      "name": "Deploy alpha leg",
      "account": "demo-account",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "1"
      ],
      "stageEnabled": {
        "type": "expression",
        "expression": "${parameters.target == 'alpha'}"
      },
      "manifests": [
        {
          "apiVersion": "batch/v1",
          "kind": "CronJob",
          "metadata": {
            "name": "enqueuer-alpha"
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
                        "image": "registry.example.com/tools/legacy-enqueuer:1.0",
                        "command": [
                          "/bin/sh",
                          "-c",
                          "echo legacy\n"
                        ],
                        "env": [
                          {
                            "name": "TARGET",
                            "value": "alpha"
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
      "refId": "old-b",
      "type": "deployManifest",
      "name": "Deploy beta leg",
      "account": "demo-account",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "1"
      ],
      "stageEnabled": {
        "type": "expression",
        "expression": "${parameters.target == 'beta'}"
      },
      "manifests": [
        {
          "apiVersion": "batch/v1",
          "kind": "CronJob",
          "metadata": {
            "name": "enqueuer-beta"
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
                        "image": "registry.example.com/tools/legacy-enqueuer:1.0",
                        "command": [
                          "/bin/sh",
                          "-c",
                          "echo legacy\n"
                        ],
                        "env": [
                          {
                            "name": "TARGET",
                            "value": "beta"
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
