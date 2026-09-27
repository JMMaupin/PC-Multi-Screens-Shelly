"""Publish a GitHub release: tag v<version>, notes, and the executable.

Run through push-release.cmd. The version is the one in
shelly_screens/__init__.py; the notes come from release-notes/<version>.md,
where `{version}` and `{sha256}` are filled in.

Everything that could publish a wrong release is checked first, since a
published release is public and may be downloaded at once:
- the working tree is clean and `main` matches GitHub: the tag points at
  exactly the code that was pushed;
- the tag does not exist yet;
- the executable is rebuilt from these sources, never taken as found in
  dist\\, where an older build may sit;
- the notes exist;
- and the publication is confirmed by hand.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from shelly_screens import __version__, product  # noqa: E402

BRANCH = "main"
NOTES_DIR = ROOT / "release-notes"


def run(*command: str, check: bool = True) -> str:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        fail(f"{' '.join(command)} failed:\n{result.stderr or result.stdout}")
    return result.stdout.strip()


def fail(message: str) -> None:
    print(f"\nRelease aborted: {message}")
    sys.exit(1)


def check_tools() -> None:
    if shutil.which("gh") is None:
        fail("the GitHub CLI (gh) is not installed: https://cli.github.com")
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        fail("gh is not signed in: run `gh auth login` first")


def check_repository(tag: str) -> str:
    """The commit to tag, once the tree is clean and pushed."""
    if run("git", "status", "--porcelain"):
        fail("uncommitted changes. Commit and push them first.")
    current = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    if current != BRANCH:
        fail(f"on branch '{current}', releases are made from '{BRANCH}'.")
    run("git", "fetch", "--quiet", "origin", BRANCH)
    head = run("git", "rev-parse", "HEAD")
    remote = run("git", "rev-parse", f"origin/{BRANCH}")
    if head != remote:
        fail(f"'{BRANCH}' differs from GitHub. Push (or pull) first.")
    if run("git", "ls-remote", "--tags", "origin", tag):
        fail(f"tag {tag} already exists on GitHub. Raise the version in "
             "shelly_screens/__init__.py for a new release.")
    return head


def build() -> Path:
    print("Building the executable from these sources...")
    result = subprocess.run(["cmd", "/c", str(ROOT / "build.cmd")], cwd=ROOT)
    if result.returncode != 0:
        fail("the build failed.")
    exe = ROOT / "dist" / f"ShellyPCScreens-{__version__}.exe"
    if not exe.exists():
        fail(f"{exe} was not produced.")
    return exe


def notes(exe: Path) -> Path:
    source = NOTES_DIR / f"{__version__}.md"
    if not source.exists():
        fail(f"no release notes: write {source.relative_to(ROOT)} "
             "(it may use {version} and {sha256}).")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    text = source.read_text(encoding="utf-8")
    if "{sha256}" in text:
        text = text.replace("{sha256}", digest)
    else:
        text += f"\n\nSHA-256 of `{exe.name}`: `{digest}`\n"
    text = text.replace("{version}", __version__)
    handle, name = tempfile.mkstemp(prefix="release-notes-", suffix=".md")
    with open(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
    return Path(name)


def main() -> None:
    tag = f"v{__version__}"
    title = f"{product.APP_NAME} {__version__}"
    print(f"Release {tag} of {product.APP_NAME}")
    check_tools()
    commit = check_repository(tag)
    exe = build()
    notes_file = notes(exe)
    try:
        print(f"\n  Tag     : {tag} on {commit[:10]} ({BRANCH})")
        print(f"  Title   : {title}")
        print(f"  File    : {exe.name} ({exe.stat().st_size / 1_048_576:.1f} MB)")
        print(f"  Notes   : release-notes/{__version__}.md")
        print(f"  Page    : {product.RELEASES_URL}")
        answer = input("\nPublish this release publicly? Type 'yes' to confirm: ")
        if answer.strip().lower() != "yes":
            fail("not confirmed, nothing was published.")
        run("gh", "release", "create", tag, str(exe),
            "--target", commit, "--title", title, "--notes-file", str(notes_file))
    finally:
        notes_file.unlink(missing_ok=True)
    print(f"\nPublished: {product.RELEASES_URL}/tag/{tag}")


if __name__ == "__main__":
    main()
