{
  "cwlVersion": "v1.2",
  "class": "CommandLineTool",
  "id": "probe",
  "baseCommand": ["python3"],
  "inputs": {
    "script": {"type": "File", "inputBinding": {"position": 0}},
    "run_id": {"type": "string", "inputBinding": {"position": 1}},
    "step_path": {"type": "string", "inputBinding": {"position": 2}},
    "ordinal": {"type": "int", "inputBinding": {"position": 3}},
    "value": {"type": "int", "inputBinding": {"position": 4}},
    "delay": {"type": "int", "inputBinding": {"position": 5}},
    "fail_once": {"type": "string", "inputBinding": {"position": 6}},
    "started_file": {"type": "string", "default": "none", "inputBinding": {"position": 7}}
  },
  "stdout": "probe.json",
  "outputs": {"record": {"type": "File", "outputBinding": {"glob": "probe.json"}}},
  "requirements": {"ResourceRequirement": {"coresMin": 1, "ramMin": 128}}
}
