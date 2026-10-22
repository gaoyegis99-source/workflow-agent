"""Diagnostic Platform Worker that reproduces the pre-R2 default CWL resources."""


def main():
    from platform_core.application import workflow_compiler

    original = workflow_compiler.compile_package

    def without_coordination_resources(*args, **kwargs):
        compiled = original(*args, **kwargs)
        for process in compiled["files"].values():
            requirements = process.get("requirements", {})
            requirements.pop("ResourceRequirement", None)
        return compiled

    workflow_compiler.compile_package = without_coordination_resources
    from platform_infrastructure.worker import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()
