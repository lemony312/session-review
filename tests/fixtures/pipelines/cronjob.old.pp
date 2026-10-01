{
  "name": "sample-enqueue",
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
      "refId": "2",
      "type": "deployManifest",
      "name": "Deploy enqueuer",
      "account": "demo-account",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "1"
      ],
      "manifests": [
        {
          "apiVersion": "batch/v1",
          "kind": "CronJob",
          "metadata": {
            "name": "enqueuer"
          },
          "spec": {
            "schedule": "0 * * * *",
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
