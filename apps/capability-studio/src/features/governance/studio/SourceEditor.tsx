import Editor, { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import "monaco-editor/esm/vs/language/json/monaco.contribution";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import YamlWorker from "monaco-yaml/yaml.worker?worker";
import { configureMonacoYaml } from "monaco-yaml";

self.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    if (label === "json") return new JsonWorker();
    if (label === "yaml") return new YamlWorker();
    return new EditorWorker();
  },
};

loader.config({ monaco });
configureMonacoYaml(monaco, {
  enableSchemaRequest: false,
  completion: true,
  validate: true,
  hover: true,
  format: {},
  schemas: [],
});

export default function SourceEditor({ value, format, readOnly, onChange }: {
  value: string;
  format: "JSON" | "YAML";
  readOnly?: boolean;
  onChange: (value: string) => void;
}) {
  return <div className="monaco-shell"><Editor
    height="100%"
    language={format === "JSON" ? "json" : "yaml"}
    value={value}
    onChange={(next) => onChange(next ?? "")}
    options={{
      readOnly,
      editContext: false,
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
      lineHeight: 21,
      wordWrap: "on",
      scrollBeyondLastLine: false,
      renderValidationDecorations: "on",
      ariaLabel: "工作流源码",
      tabSize: 2,
    }}
  /></div>;
}
