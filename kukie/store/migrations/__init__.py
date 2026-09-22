"""Alembic 마이그레이션 — 표 모양의 변경 이력 (issue #67).

`create_all` 은 없는 표만 만들어서, 이미 있는 DB 의 열·인덱스 변경이 안 나갔다. 이제 켜질 때
`kukie.store.db` 가 여기 `versions/` 의 리비전을 순서대로 적용한다. 새 DB 도 옛 DB 도 같은 길이다.

모델(`kukie/store/models.py`)을 바꾸면 리비전을 **같이** 만든다 — 만드는 법은 루트의 alembic.ini 머리말.
`tests/test_migrations.py` 가 모델과 마이그레이션 결과가 같은지 대조한다.
"""
