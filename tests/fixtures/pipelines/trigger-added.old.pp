{
  "name": "sample-docs-sync",
  "application": "demo-app",
  "triggers": [],
  "stages": [
    {
      "refId": "1",
      "type": "runJobManifest",
      "name": "Sync",
      "moniker": {
        "app": "demo-app"
      },
      "requisiteStageRefIds": [],
      "manifest": {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
          "name": "sync"
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
                    "echo sync\n"
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
