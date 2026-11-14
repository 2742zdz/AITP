#!/usr/bin/env python3
"""Finds, downloads and checks this repository's GitHub releases (gh api).

  github_release.py count --repository OWNER/NAME --tag TAG
      Prints how many releases (drafts included) have tag TAG.

  github_release.py fetch --repository OWNER/NAME --tag TAG --sha SHA
                          --dist DIR --release-json FILE --github-output FILE
                          --asset NAME [--asset NAME ...]
      Only the one release (draft or published) of TAG, and only when a
      successful tag run (event push) of the release workflow built TAG at
      SHA: downloads exactly the named assets into the new directory DIR,
      writes the release JSON to FILE and appends id=, state= (draft or
      published) and run_id= (that tag run) to the GitHub output file.

  github_release.py make-latest --tag TAG --latest LATEST
      Prints "true" when TAG's version is higher than that of LATEST, the
      tag of the current latest release (empty: none), else "false": a
      release of an older version never becomes the latest (updates read
      the latest release).

  github_release.py verify --dist DIR --tag TAG --state draft|published
                           --asset NAME [--asset NAME ...] < release.json
      The release has tag TAG, is a draft (not immutable) or published and
      immutable, is no prerelease, and holds exactly the named assets, each
      uploaded with the size and SHA-256 digest of DIR/NAME.

Exits 0 when the release passes, 1 otherwise (the reason on stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


WORKFLOW_NAME = "Release"
WORKFLOW_PATH = ".github/workflows/release.yml"
SHA = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class CheckError(Exception):
    pass


def require(condition: object, message: str) -> None:
    if not condition:
        raise CheckError(message)


def local_file(path: Path) -> tuple[int, str]:
    require(path.is_file() and not path.is_symlink(), f"missing or not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def gh(*arguments: str) -> bytes:
    result = subprocess.run(["gh", "api", *arguments], capture_output=True, check=False)
    require(result.returncode == 0,
            f"gh api {arguments[-1]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def tag_releases(repository: str, tag: str) -> list[dict]:
    pages = json.loads(gh("--paginate", "--slurp", f"repos/{repository}/releases?per_page=100"))
    require(isinstance(pages, list) and all(isinstance(page, list) for page in pages), "unexpected release pages")
    return [release for page in pages for release in page
            if isinstance(release, dict) and release.get("tag_name") == tag]


def tag_run(runs: object, tag: str, sha: str) -> dict:
    """The latest successful tag run of the release workflow for `tag` at `sha`."""
    require(isinstance(runs, list), "the workflow runs are not a list")
    matching = [
        run for run in runs
        if isinstance(run, dict)
        and run.get("name") == WORKFLOW_NAME
        and run.get("path") == WORKFLOW_PATH
        and run.get("event") == "push"
        and run.get("head_branch") == tag
        and run.get("head_sha") == sha
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and isinstance(run.get("id"), int)
    ]
    require(matching, f"no successful {WORKFLOW_NAME!r} tag run of {tag} at {sha}")
    return max(matching, key=lambda run: run["id"])


def fetch(args) -> None:
    require(TAG.fullmatch(args.tag) is not None, f"{args.tag!r} is not a vX.Y.Z tag")
    require(SHA.fullmatch(args.sha) is not None, "the commit is not a full SHA")
    require(not args.dist.exists(), f"{args.dist} already exists")
    names = check_names(args.asset)
    pages = json.loads(gh("--paginate", "--slurp",
                          f"repos/{args.repository}/actions/workflows/{Path(WORKFLOW_PATH).name}/runs"
                          f"?event=push&head_sha={args.sha}&per_page=100"))
    require(isinstance(pages, list) and all(isinstance(page, dict) for page in pages), "unexpected run pages")
    runs = []
    for page in pages:
        require(isinstance(page.get("workflow_runs"), list), "unexpected run page")
        runs.extend(page["workflow_runs"])
    run = tag_run(runs, args.tag, args.sha)
    releases = tag_releases(args.repository, args.tag)
    require(len(releases) == 1, f"expected exactly one release of {args.tag}, found {len(releases)}")
    release = releases[0]
    require(isinstance(release.get("id"), int) and isinstance(release.get("draft"), bool), "unexpected release JSON")
    assets = release.get("assets")
    require(isinstance(assets, list), "the release lists no assets")
    plan = sorted((asset.get("name"), asset.get("id")) for asset in assets if isinstance(asset, dict))
    require(len(plan) == len(assets) and all(isinstance(identifier, int) for _, identifier in plan),
            "an asset has no numeric id")
    found = [name for name, _ in plan]
    require(len(set(found)) == len(found) and sorted(found) == sorted(names),
            f"the release has assets {found}, expected {sorted(names)}")
    args.dist.mkdir(parents=True)
    for name, identifier in plan:
        data = gh("-H", "Accept: application/octet-stream", f"repos/{args.repository}/releases/assets/{identifier}")
        (args.dist / name).write_bytes(data)
    args.release_json.write_text(json.dumps(release), encoding="utf-8")
    state = "draft" if release["draft"] else "published"
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"id={release['id']}\nstate={state}\nrun_id={run['id']}\n")
    print(f"fetched the {state} release {release['id']} of {args.tag} ({len(plan)} assets), built by run {run['id']}")


def version(tag: str) -> tuple[int, int, int]:
    match = TAG.fullmatch(tag)
    require(match is not None, f"{tag!r} is not a vX.Y.Z tag")
    return tuple(int(part) for part in match.groups())


def make_latest(tag: str, latest: str) -> bool:
    """Whether publishing `tag` should make it the latest release, given
    the current latest release's tag (empty when there is none)."""
    if not latest:
        version(tag)
        return True
    require(version(tag) != version(latest), f"{tag} is already the latest release")
    return version(tag) > version(latest)


def check_names(names: list[str]) -> list[str]:
    require(names and len(set(names)) == len(names), "the asset list is empty or repeats a name")
    for name in names:
        require(NAME.fullmatch(name) is not None, f"bad asset name {name!r}")
    return names


def verify(release: dict, dist: Path, tag: str, state: str, names: list[str]) -> None:
    check_names(names)
    require(release.get("tag_name") == tag, f"the release has tag {release.get('tag_name')!r}, not {tag!r}")
    require(release.get("prerelease") is False, "the release is a prerelease")
    if state == "draft":
        require(release.get("draft") is True, "the release is not a draft")
        require(release.get("immutable") is not True, "the draft is already immutable")
    else:
        require(release.get("draft") is False, "the release is still a draft")
        require(release.get("immutable") is True, "the published release is not immutable")
    assets = release.get("assets")
    require(isinstance(assets, list), "the release lists no assets")
    found = [asset.get("name") for asset in assets]
    require(len(set(found)) == len(found), f"the release repeats an asset: {sorted(found)}")
    require(sorted(found) == sorted(names), f"the release has assets {sorted(found)}, expected {sorted(names)}")
    for asset in assets:
        name = asset["name"]
        size, digest = local_file(dist / name)
        require(asset.get("state") == "uploaded", f"asset {name} is {asset.get('state')!r}, not uploaded")
        require(asset.get("size") == size, f"asset {name} has {asset.get('size')} bytes, the local file {size}")
        require(asset.get("digest") == f"sha256:{digest}",
                f"asset {name} has digest {asset.get('digest')!r}, the local file sha256:{digest}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    count = commands.add_parser("count")
    count.add_argument("--repository", required=True)
    count.add_argument("--tag", required=True)
    download = commands.add_parser("fetch")
    download.add_argument("--repository", required=True)
    download.add_argument("--tag", required=True)
    download.add_argument("--sha", required=True)
    download.add_argument("--dist", required=True, type=Path)
    download.add_argument("--release-json", required=True, type=Path)
    download.add_argument("--github-output", required=True, type=Path)
    download.add_argument("--asset", action="append", required=True)
    latest = commands.add_parser("make-latest")
    latest.add_argument("--tag", required=True)
    latest.add_argument("--latest", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--dist", required=True, type=Path)
    check.add_argument("--tag", required=True)
    check.add_argument("--state", required=True, choices=("draft", "published"))
    check.add_argument("--asset", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "count":
            require(TAG.fullmatch(args.tag) is not None, f"{args.tag!r} is not a vX.Y.Z tag")
            print(len(tag_releases(args.repository, args.tag)))
        elif args.command == "fetch":
            fetch(args)
        elif args.command == "make-latest":
            print("true" if make_latest(args.tag, args.latest) else "false")
        else:
            release = json.load(sys.stdin)
            require(isinstance(release, dict), "the release JSON is not an object")
            verify(release, args.dist, args.tag, args.state, args.asset)
            print(f"release {args.tag} ({args.state}) holds exactly the {len(args.asset)} local files")
    except (CheckError, ValueError, KeyError, OSError) as error:
        print(f"github_release.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
