"""Private 스킬 — 혼자 보는 대화. 읽기 툴(kubectl 5종 + 레포 3종)만, 변경 툴은 아예 노출하지 않는다 (DURO-100)."""
from kukie_agent import prompts
from kukie_agent.skills.base import Skill

PROMPT = prompts.load("private")

SKILL = Skill(
    name="Private",
    prompt=PROMPT,
    # 추가 툴 없음 — 공통 읽기 툴만. private 방의 변경 금지(기획 05 §3)를 툴 목록으로 보장한다
)
