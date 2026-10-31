# ComfyUI H3 workflow contract

This directory intentionally does not contain a fabricated MiniMax H3 graph. Export the
API-format workflow from the installed ComfyUI instance, verify its nodes through
`/object_info`, and add a top-level `_video_factory` mapping alongside the `prompt` graph.

Required envelope:

```json
{
  "_video_factory": {
    "name": "installed-workflow-name",
    "version": "verified-version",
    "prompt_path": ["NODE_ID", "inputs", "text"],
    "negative_prompt_path": ["NODE_ID", "inputs", "text"],
    "duration_path": ["NODE_ID", "inputs", "duration"],
    "seed_path": ["NODE_ID", "inputs", "seed"],
    "reference_paths": []
  },
  "prompt": {
    "NODE_ID": {
      "class_type": "ACTUAL_INSTALLED_NODE_CLASS",
      "inputs": {}
    }
  }
}
```

Do not copy the illustrative node names above into a production graph. Reference upload is
kept disabled until the exact installed workflow's upload and node mapping is implemented
and verified. Without this configuration the API reports the real provider as not ready;
it never silently falls back to demo mode.

