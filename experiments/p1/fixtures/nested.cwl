{
  "cwlVersion": "v1.2",
  "class": "Workflow",
  "id": "nested",
  "requirements": {"SubworkflowFeatureRequirement": {}, "ScatterFeatureRequirement": {}},
  "inputs": {
    "script": "File", "run_id": "string", "step_path": "string",
    "ordinals": "int[]", "values": "int[]", "delay": "int", "fail_once": "string",
    "started_file": {"type": "string", "default": "none"}
  },
  "steps": {
    "outer": {
      "run": "scatter.cwl",
      "in": {
        "script": "script", "run_id": "run_id", "step_path": "step_path",
        "ordinals": "ordinals", "values": "values", "delay": "delay", "fail_once": "fail_once",
        "started_file": "started_file"
      },
      "out": ["records"]
    }
  },
  "outputs": {"records": {"type": "File[]", "outputSource": "outer/records"}}
}
