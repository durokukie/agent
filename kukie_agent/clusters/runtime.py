"""실행 직전에 임시 kubeconfig 파일을 만든다 (기획 04 §8 "실행 방식").

원칙 넷.
  - 서버의 기본 kubeconfig 나 공용 환경변수는 건드리지 않는다. 요청마다 자기 파일을 쓴다
  - 파일은 소유자만 읽을 수 있게(0600) 만들고, 블록을 벗어나면 지운다
  - 블록을 벗어나지 못하고 죽은(SIGKILL·OOM) 프로세스의 파일은 다음 기동 때 지운다 — remove_orphaned
  - 자격증명은 이 파일 밖으로 나가지 않는다 — 로그·LLM 입력·승인 카드·오류 메시지에 싣지 않는다
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CLUSTER_NAME = "kukie"
USER_NAME = "kukie"

TEMP_DIR = Path(tempfile.gettempdir())
PREFIX = "kukie-kubeconfig-"
# kukie-kubeconfig-<만든 프로세스 pid>-<mkstemp 임의 글자>.yaml. pid 가 없는 이름은 pid 를 넣기 전에 만든 것이다
_NAME = re.compile(rf"{re.escape(PREFIX)}(?:(\d+)-)?[a-z0-9_]+\.yaml")


def build(
    *,
    context_name: str,
    api_server: str,
    ca_data: str | None,
    namespace: str,
    credential: dict[str, str],
    insecure: bool = False,
) -> str:
    """등록된 값으로 kubeconfig 본문을 만든다. exec 도 파일 경로도 쓰지 않는다."""
    cluster: dict[str, object] = {"server": api_server}
    if ca_data:
        cluster["certificate-authority-data"] = ca_data
    if insecure:
        cluster["insecure-skip-tls-verify"] = True

    user: dict[str, object] = {}
    if "token" in credential:
        user["token"] = credential["token"]
    else:
        user["client-certificate-data"] = credential["client_certificate_data"]
        user["client-key-data"] = credential["client_key_data"]

    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": CLUSTER_NAME, "cluster": cluster}],
            "users": [{"name": USER_NAME, "user": user}],
            "contexts": [
                {
                    "name": context_name,
                    "context": {
                        "cluster": CLUSTER_NAME,
                        "user": USER_NAME,
                        "namespace": namespace,
                    },
                }
            ],
            "current-context": context_name,
        },
        sort_keys=False,
    )


@contextmanager
def temporary_kubeconfig(body: str) -> Iterator[Path]:
    """본문을 0600 임시 파일에 쓰고 경로를 준다. 블록을 벗어나면 반드시 지운다.

    이름에 이 프로세스의 pid 를 넣는다 — finally 가 못 돈 파일을 다음 기동이 주인으로 가려 지운다 (remove_orphaned).
    """
    handle, name = tempfile.mkstemp(prefix=f"{PREFIX}{os.getpid()}-", suffix=".yaml", dir=TEMP_DIR)
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(body)
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)


def remove_orphaned() -> int:
    """주인 프로세스가 죽어 남은 임시 kubeconfig 를 지우고 지운 개수를 준다. 기동 때 한 번 부른다 (server.py lifespan).

    temporary_kubeconfig 의 finally 는 SIGKILL 에는 돌지 않는다. kubectl 이 도는(막힌) 중에 `docker compose restart`·
    OOM·재시작 정책으로 죽으면 평문 자격증명이 든 파일이 남고, 컨테이너를 새로 만들기 전까지 /tmp 에 그대로 있다.

    주인은 이름의 pid 로 가린다.
      - 살아 있는 다른 프로세스(같은 컴에서 띄운 다른 agent·테스트)의 파일은 kubectl 이 읽는 중일 수 있어 둔다
      - 이 프로세스 pid 의 파일은 지운다 — 기동 때는 아직 아무 파일도 만들지 않았으므로 같은 pid 를 받았던 이전
        프로세스의 것이다 (컨테이너의 uvicorn 은 다시 떠도 PID 1). 그래서 요청을 받기 시작한 뒤에는 부르지 않는다
      - pid 가 없는 예전 이름은 주인을 알 수 없어 지운다
    """
    removed = 0
    for path in TEMP_DIR.glob(f"{PREFIX}*.yaml"):
        match = _NAME.fullmatch(path.name)
        if match is None or (match[1] is not None and _alive(int(match[1]))):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # 경로만 남긴다 — 본문(자격증명)은 로그에 싣지 않는다
            logger.warning("남은 임시 kubeconfig 를 지우지 못했다: %s", path, exc_info=True)
            continue
        removed += 1
    return removed


def _alive(pid: int) -> bool:
    """그 pid 의 프로세스가 있는가. 이 프로세스 자신은 없다고 본다 (remove_orphaned 참고)."""
    if pid == os.getpid():
        return False
    if os.name != "posix":
        return True             # Windows 의 os.kill(pid, 0) 은 확인이 아니라 신호를 보낸다 — 모르면 둔다
    try:
        os.kill(pid, 0)         # 신호 0 은 보내지 않고 있는지만 본다
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True             # 다른 사용자의 살아 있는 프로세스
    return True
