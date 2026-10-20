"""Private 스킬 — 혼자 보는 대화. 읽기 5종만, 변경 툴은 아예 노출하지 않는다 (DURO-100)."""
from kukie_agent.skills.base import Skill

PROMPT = """이 대화는 Private 대화다. 클러스터를 읽을 수만 있고 바꿀 수는 없다.
변경을 요청받으면 시도하지 말고, 변경은 Shared 대화에서만 할 수 있다고 알려라.
대신 사용자가 직접 터미널에서 실행할 kubectl 명령과 각 플래그의 의미를 안내해라."""

SKILL = Skill(
    name="Private",
    prompt=PROMPT,
    # 추가 툴 없음 — 공통 읽기 툴만. private 방의 변경 금지(기획 05 §3)를 툴 목록으로 보장한다
)
