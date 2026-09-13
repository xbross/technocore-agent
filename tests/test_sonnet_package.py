"""Le paquet officiel n'est utilise qu'apres verification des hashes epingles dans LAUNCH.md."""
import hashlib

import pytest

from technocore_agent.sonnet import package


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make(tmp_path):
    (tmp_path / "cmudict.dict").write_text("moon M UW1 N\n", "utf-8")
    (tmp_path / "sonnet_validate.py").write_text("def validate_word(t, d, l):\n    return 1\n", "utf-8")
    return {"cmudict.dict": _sha(tmp_path / "cmudict.dict"),
            "sonnet_validate.py": _sha(tmp_path / "sonnet_validate.py")}


def test_verify_package_accepts_matching_hashes(tmp_path):
    hashes = _make(tmp_path)
    package.verify_package(tmp_path, hashes)  # ne leve pas


def test_verify_package_rejects_tampered_file(tmp_path):
    hashes = _make(tmp_path)
    (tmp_path / "cmudict.dict").write_text("moon M UW1 N\nevil X\n", "utf-8")
    with pytest.raises(package.PackageError, match="cmudict.dict"):
        package.verify_package(tmp_path, hashes)


def test_verify_package_rejects_missing_file(tmp_path):
    hashes = _make(tmp_path)
    (tmp_path / "sonnet_validate.py").unlink()
    with pytest.raises(package.PackageError, match="sonnet_validate.py"):
        package.verify_package(tmp_path, hashes)


def test_load_validator_refuses_before_import_when_hash_differs(tmp_path):
    hashes = _make(tmp_path)
    (tmp_path / "sonnet_validate.py").write_text("import os; os.system('echo pwned')\n", "utf-8")
    with pytest.raises(package.PackageError):
        package.load_validator(tmp_path, hashes)


def test_load_validator_imports_official_module_after_check(tmp_path):
    hashes = _make(tmp_path)
    mod = package.load_validator(tmp_path, hashes)
    assert mod.validate_word("moon", "did", {}) == 1


def test_fetch_package_downloads_pinned_files_and_rejects_bad_content(tmp_path):
    good = b"moon M UW1 N\n"
    hashes = {"cmudict.dict": hashlib.sha256(good).hexdigest()}
    urls = []

    def fetch(url):
        urls.append(url)
        return good

    package.fetch_package(tmp_path / "pkg", "abc123", hashes, fetch=fetch)
    assert urls == [package.RAW_BASE + "abc123/cmudict.dict"]
    assert (tmp_path / "pkg" / "cmudict.dict").read_bytes() == good
    with pytest.raises(package.PackageError):
        package.fetch_package(tmp_path / "pkg2", "abc123", hashes, fetch=lambda u: b"tampered\n")
    assert not (tmp_path / "pkg2" / "cmudict.dict").exists()
