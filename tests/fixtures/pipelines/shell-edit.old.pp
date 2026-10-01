{
  "name": "sample-notify",
  "application": "demo-app",
  "triggers": [],
  "stages": [
    {
      "refId": "1",
      "type": "runJobManifest",
      "name": "Notify success",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [],
      "manifest": {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
          "name": "notify-success"
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
                    "set -eu\nURL=\"${WEBHOOK_URL}\"\nSTATUS=\"${1:-unknown}\"\necho \"starting notify\"\necho \"step 1\"\necho \"step 2\"\necho \"step 3\"\necho \"step 4\"\necho \"step 5\"\necho \"step 6\"\necho \"step 7\"\necho \"step 8\"\necho \"step 9\"\necho \"step 10\"\necho \"step 11\"\necho \"step 12\"\necho \"step 13\"\necho \"step 14\"\necho \"step 15\"\necho \"step 16\"\necho \"step 17\"\necho \"step 18\"\necho \"step 19\"\necho \"step 20\"\necho \"step 21\"\necho \"step 22\"\necho \"step 23\"\necho \"step 24\"\npayload=$(printf '{\"text\":\"deploy %s\"}' \"$STATUS\")\ncurl -sS -X POST -d \"$payload\" \"$URL\"\necho done\n"
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
      "name": "Notify failure",
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
          "name": "notify-failure"
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
                    "set -eu\nURL=\"${WEBHOOK_URL}\"\nSTATUS=\"${1:-unknown}\"\necho \"starting notify\"\necho \"step 1\"\necho \"step 2\"\necho \"step 3\"\necho \"step 4\"\necho \"step 5\"\necho \"step 6\"\necho \"step 7\"\necho \"step 8\"\necho \"step 9\"\necho \"step 10\"\necho \"step 11\"\necho \"step 12\"\necho \"step 13\"\necho \"step 14\"\necho \"step 15\"\necho \"step 16\"\necho \"step 17\"\necho \"step 18\"\necho \"step 19\"\necho \"step 20\"\necho \"step 21\"\necho \"step 22\"\necho \"step 23\"\necho \"step 24\"\npayload=$(printf '{\"text\":\"deploy %s\"}' \"$STATUS\")\ncurl -sS -X POST -d \"$payload\" \"$URL\"\necho done\n"
                  ],
                  "env": []
                }
              ]
            }
          }
        }
      }
    }
  ]
}
