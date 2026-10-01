{
  "name": "sample-migrate",
  "application": "demo-app",
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
      "type": "runJobManifest",
      "name": "Migrate",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "1"
      ],
      "manifest": {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
          "name": "migrate"
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
                    "echo migrate\n"
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
      "refId": "3",
      "type": "runJobManifest",
      "name": "Verify",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "2"
      ],
      "manifest": {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
          "name": "verify"
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
                    "echo verify\n"
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
      "refId": "4",
      "type": "wait",
      "name": "Cooldown",
      "waitTime": 5,
      "requisiteStageRefIds": [
        "3"
      ]
    },
    {
      "refId": "5",
      "type": "runJobManifest",
      "name": "Report",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [
        "4"
      ],
      "manifest": {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
          "name": "report"
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
                    "echo report\n"
                  ],
                  "env": []
                }
              ]
            }
          }
        }
      }
    }
  ],
  "triggers": [],
  "parameterConfig": []
}
