from scripts.maintenance.check_architecture_boundaries import scan


def test_frontend_features_do_not_reach_into_each_other() -> None:
    assert not [
        finding for finding in scan() if finding["code"] == "FRONTEND_FEATURE_REVERSE_IMPORT"
    ]
