"""PR 이 바꾼 파일을 보고 CI 의 test · e2e job 을 돌릴지 정한다.

표에 없는 경로는 돌린다. 건너뛰는 경로만 적는다 — 새 파일이 생겨도 테스트가 빠지지 않게 하려는 것이다.
PR 이 아닌 이벤트(develop · main push)와 바뀐 파일을 못 읽은 경우에도 둘 다 돌린다.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable

# test 도 e2e 도 보지 않는 경로. kukie_agent/prompts 의 .md 는 코드이므로 여기 넣지 않는다.
# helm_render/ 는 helm-render.yml 이 따로 시험한다
UNTESTED_PREFIXES = ("docs/", "helm_render/")
UNTESTED_FILES = {
    ".github/workflows/claude.yml",
    ".github/workflows/claude-review.yml",
    ".github/workflows/helm-render.yml",
}

# e2e(가드레일의 kubectl 실행)가 지나지 않는 경로. gitops · manifests 는 guardrail/hook.py 가 import 하므로 넣지 않는다
NO_E2E_PREFIXES = ("tests/clusters/", "tests/core/", "tests/fixtures/", "tests/gitops/", "tests/guardrail/",
                   "tests/manifests/")
NO_E2E_FILES = {"kukie_agent/clusters/gpu.py", "kukie_agent/clusters/overview.py", "kukie_agent/clusters_api.py"}


def _untested(path: str) -> bool:
    root_markdown = "/" not in path and path.endswith(".md")
    return root_markdown or path.startswith(UNTESTED_PREFIXES) or path in UNTESTED_FILES


def _no_e2e(path: str) -> bool:
    return _untested(path) or path.startswith(NO_E2E_PREFIXES) or path in NO_E2E_FILES


def scope(paths: Iterable[str]) -> dict[str, bool]:
    """바뀐 파일 목록 → job 마다 돌릴지. 목록이 비었으면 뭔가 잘못 읽은 것이라 돌린다."""
    changed = list(paths)
    if not changed:
        return {"test": True, "e2e": True}
    return {"test": not all(map(_untested, changed)), "e2e": not all(map(_no_e2e, changed))}


def changed_files() -> list[str]:
    """pull_request 이벤트의 HEAD 는 머지 커밋이라 첫 부모와의 차이가 PR 이 바꾸는 파일이다.

    --no-renames: 옮긴 파일의 옛 경로도 바뀐 파일로 친다. -z: 한글 파일 이름을 그대로 받는다.
    """
    out = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", "HEAD^1", "HEAD"],
                         check=True, capture_output=True, text=True).stdout
    return [path for path in out.split("\0") if path]


def main() -> None:
    paths: list[str] = []
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        try:
            paths = changed_files()
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"바뀐 파일을 못 읽어 전부 돌린다: {error}")
    result = scope(paths)
    print(f"바뀐 파일 {len(paths)}개 → {result}")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for job, run in result.items():
            output.write(f"{job}={str(run).lower()}\n")


if __name__ == "__main__":
    main()
