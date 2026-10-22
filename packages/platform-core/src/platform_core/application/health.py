from platform_core.domain.health import ProcessHealth


def read_process_health() -> ProcessHealth:
    return ProcessHealth()
