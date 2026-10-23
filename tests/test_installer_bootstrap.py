"""Exercise the bootstrap checkout step without apt, network, or host installs."""

import os
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/Twarner491/AvianVisitors.git"


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True,
                          text=True, capture_output=True).stdout.strip()


@pytest.fixture
def bootstrap(tmp_path):
    upstream = tmp_path / "upstream"
    git("init", "-b", "avian-visitors", upstream)
    scripts = upstream / "scripts"
    scripts.mkdir()
    installer = scripts / "install_birdnet.sh"
    installer.write_text('#!/bin/bash\nprintf "installer:%s\\n" "$*"\nexit "${INSTALL_RESULT:-0}"\n')
    installer.chmod(0o755)
    git("-C", upstream, "add", ".")
    git("-C", upstream, "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    station = tmp_path / "station"
    station.mkdir()
    checkout = station / "BirdNET-Pi"

    def run(*args, result=0):
        # The real checkout/install/reboot tail; omit only platform/sudo preflight.
        source = (ROOT / "newinstaller.sh").read_text()
        tail = source[source.index("branch=avian-visitors"):]
        tail = tail.replace("${HOME}", str(station)).replace("$HOME", str(station))
        tail = tail.replace(REMOTE, str(upstream))
        script = 'sudo() { [ "$*" = reboot ] && echo reboot-requested; }\n'
        script += "installer_args=(" + " ".join(map(shlex.quote, args)) + ")\n" + tail
        return subprocess.run(["bash", "-c", script], cwd=tmp_path,
                              env={**os.environ, "INSTALL_RESULT": str(result)},
                              text=True, capture_output=True)

    return run, checkout, upstream


def test_bootstrap_refuses_failed_checkout_without_running_older_installer(bootstrap):
    run, checkout, _ = bootstrap
    first = run(result=1)
    assert first.returncode != 0
    assert "installer:" in first.stdout
    assert "reboot-requested" not in first.stdout
    config = checkout / "birdnet.conf"
    config.write_text("local settings\n")
    installer = checkout / "scripts/install_birdnet.sh"
    installer.write_text(installer.read_text() + "# local customization\n")
    before = git("-C", checkout, "status", "--porcelain")
    second = run("--educators")
    assert second.returncode != 0
    assert "installer:" not in second.stdout
    assert "reboot-requested" not in second.stdout
    assert "Do not delete" in second.stderr
    assert config.read_text() == "local settings\n"
    assert git("-C", checkout, "status", "--porcelain") == before


def test_bootstrap_fresh_install_forwards_args_and_existing_install_is_refused(bootstrap):
    run, _, _ = bootstrap
    first = run("--educators")
    assert first.returncode == 0
    assert "installer:--educators" in first.stdout
    assert "reboot-requested" in first.stdout
    resumed = run(result=1)
    assert "installer:" not in resumed.stdout
    assert resumed.returncode != 0
    assert "reboot-requested" not in resumed.stdout


@pytest.mark.parametrize("kind", [
    "directory", "file", "symlink", "dangling-symlink", "worktree",
])
def test_bootstrap_refuses_unrecognized_existing_path(bootstrap, kind):
    run, checkout, upstream = bootstrap
    if kind == "directory":
        checkout.mkdir()
        (checkout / "sentinel").write_text("untouched\n")
    elif kind == "file":
        checkout.write_text("untouched\n")
    elif kind == "symlink":
        checkout.symlink_to(upstream, target_is_directory=True)
    elif kind == "dangling-symlink":
        checkout.symlink_to(upstream.with_name("missing"), target_is_directory=True)
    elif kind == "worktree":
        git("-C", upstream, "worktree", "add", "--detach", checkout)

    def snapshot():
        paths = [checkout, *checkout.rglob("*")]
        return {str(p): (os.readlink(p) if p.is_symlink() else p.read_bytes())
                for p in paths if p.is_symlink() or p.is_file()}

    before = snapshot()
    result = run()
    assert result.returncode != 0
    assert "installer:" not in result.stdout
    assert "reboot-requested" not in result.stdout
    assert snapshot() == before


def test_bootstrap_clone_failure_never_runs_installer(bootstrap):
    run, _, upstream = bootstrap
    upstream.rename(upstream.with_name("unavailable"))
    result = run()
    assert result.returncode != 0
    assert "installer:" not in result.stdout
    assert "reboot-requested" not in result.stdout
