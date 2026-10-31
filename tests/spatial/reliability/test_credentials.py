import os

import pytest
from platform_contracts.service import ServiceFault
from platform_execution.adapters.remote import CredentialProvider


def test_secret_file_requires_private_permissions(tmp_path):
    secret = tmp_path / "peer"
    secret.write_text("x" * 32)
    secret.chmod(0o644)
    provider = CredentialProvider({}, credential_dir=tmp_path)
    with pytest.raises(ServiceFault):
        provider.get("peer")
    secret.chmod(0o600)
    assert provider.get("peer") == "x" * 32


def test_secret_symlink_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.write_text("x" * 32)
    source.chmod(0o600)
    os.symlink(source, tmp_path / "peer")
    with pytest.raises(ServiceFault):
        CredentialProvider({}, credential_dir=tmp_path).get("peer")
