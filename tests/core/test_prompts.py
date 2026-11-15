"""프롬프트 버전 — trace 의 kukie.prompt.version·files 가 프롬프트가 바뀔 때만, 바뀐 파일만 따라 바뀌는가."""
from pathlib import Path

from kukie_agent import agent, prompts
from kukie_agent.guardrail import decision_guidance
from kukie_agent.skills import private, shared

PROMPTS_DIR = Path(prompts.__file__).parent


def test_코드가_쓰는_프롬프트는_파일_원문이다():
    assert agent.BASE_PROMPT == prompts.load("base")
    assert private.PROMPT == prompts.load("private")
    assert shared.PROMPT == prompts.load("shared")
    assert decision_guidance.INSTRUCTIONS == prompts.load("decision_guidance")
    # 에디터가 붙인 마지막 줄바꿈은 모델 입력에 들어가지 않는다
    assert not agent.BASE_PROMPT.endswith("\n")


def test_파일_지문은_git_blob_ID와_같다():
    """같아야 `git log --find-object=<지문>` 으로 그 내용이 들어간 커밋을 찾는다.

    git 이 알려 주는 값(`git hash-object`)과 견준다. git 을 subprocess 로 부르지 않는다 — 순수 함수라
    알려진 값이면 충분하고, 맥에서 전체 테스트 중 subprocess 가 SIGSEGV 로 죽는 일에 휘둘리지 않는다.
    """
    assert prompts.blob_id(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert prompts.blob_id(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_한_파일만_바꾸면_그_지문과_합친_버전만_바뀐다(tmp_path):
    (tmp_path / "a.md").write_text("하나\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("둘\n", encoding="utf-8")
    before, files_before = prompts.compute(tmp_path)

    (tmp_path / "b.md").write_text("둘을 고침\n", encoding="utf-8")
    after, files_after = prompts.compute(tmp_path)

    assert before.startswith("p-") and before != after
    assert files_after["a.md"] == files_before["a.md"]
    assert files_after["b.md"] != files_before["b.md"]


def test_내용이_같으면_버전도_같다(tmp_path):
    """프롬프트와 상관없는 수정(다른 폴더·파일 시각)에는 버전이 그대로다 — 커밋 SHA 와 다른 점."""
    (tmp_path / "a.md").write_text("하나\n", encoding="utf-8")
    first = prompts.compute(tmp_path)
    (tmp_path / "notes.txt").write_text("md 가 아닌 파일\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("하나\n", encoding="utf-8")
    assert prompts.compute(tmp_path) == first


def test_resource_속성은_버전과_파일별_짧은_지문이다():
    attrs = prompts.resource_attributes()
    version, files = prompts.compute(PROMPTS_DIR)
    assert attrs["kukie.prompt.version"] == version
    assert attrs["kukie.prompt.files"] == ",".join(f"{name}={blob[:12]}" for name, blob in files.items())
