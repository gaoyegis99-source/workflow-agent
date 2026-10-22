"""Runtime adapter failures kept separate from business-rule failures."""


class RuntimeAdapterError(RuntimeError):
    code = "RUNTIME_ERROR"


class RuntimeNotFound(RuntimeAdapterError):
    code = "RUNTIME_NOT_FOUND"


class RuntimeConflict(RuntimeAdapterError):
    code = "RUNTIME_CONFLICT"


class RuntimeUnavailable(RuntimeAdapterError):
    code = "RUNTIME_UNAVAILABLE"


class RuntimeCorrupt(RuntimeAdapterError):
    code = "RUNTIME_CORRUPT"


class RuntimeStopped(RuntimeAdapterError):
    code = "RUNTIME_STOPPED"


class ModelOutputInvalid(RuntimeAdapterError):
    code = "MODEL_OUTPUT_INVALID"
