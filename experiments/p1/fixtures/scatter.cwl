{
  "cwlVersion": "v1.2",
  "class": "Workflow",
  "id": "identity",
  "requirements": {"ScatterFeatureRequirement": {}},
  "inputs": {
    "script": "File", "run_id": "string", "step_path": "string",
    "ordinals": "int[]", "values": "int[]", "delay": "int", "fail_once": "string",
    "started_file": {"type": "string", "default": "none"}
  },
  "steps": {
    "scatter": {
      "run": "tool.cwl",
      "in": {
        "script": "script", "run_id": "run_id", "step_path": "step_path",
        "ordinal": "ordinals", "value": "values", "delay": "delay", "fail_once": "fail_once",
        "started_file": "started_file"
      },
      "scatter": ["ordinal", "value"],
      "scatterMethod": "dotproduct",
      "out": ["record"]
    }
  },
  "outputs": {"records": {"type": "File[]", "outputSource": "scatter/record"}}
}
