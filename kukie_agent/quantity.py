"""쿠버네티스 수량 — 문법 하나를 manifests(리소스 값 검증)와 clusters/gpu(PVC 크기)가 같이 쓴다."""
from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any

#: 숫자에 지수(1e3)나 이진 단위(Ki~Ei)·십진 단위(m, k~E) 하나. 첫 묶음은 숫자, 둘째는 단위·지수다
QUANTITY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([eE][-+]?[0-9]+|m|k|Ki|Mi|Gi|Ti|Pi|Ei|M|G|T|P|E)?\Z")   # \Z — 끝 줄바꿈도 받지 않는다

_UNITS: dict[str, int | Fraction] = {
    "": 1, "m": Fraction(1, 1000), "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6,
    "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5, "E": 1000**6}
#: 쿠버네티스는 수량을 int64 로 자른다 — 그보다 큰 값은 apiserver 가 만들지 않는다
MAX_INT = 2**63 - 1
#: 숫자 자릿수·지수 상한. 넘으면 apiserver 가 만든 값이 아니고, 큰 정수를 만들지 않는다
_MAX_DIGITS = 30
_MAX_EXPONENT = 30


def to_int(value: Any) -> int | None:
    """"100Gi"·"40G"·"1073741824"·"1e9" 를 정수로(소수는 올린다 — 쿠버네티스와 같다). 모르는 모양이거나 int64 를 넘으면 None."""
    match = QUANTITY.match(value) if isinstance(value, str) else None
    if not match or len(match[1]) > _MAX_DIGITS:
        return None
    suffix = match[2] or ""
    if suffix in _UNITS:
        scale: int | Fraction = _UNITS[suffix]
    else:                                                   # 단위가 아니면 지수다 — "Ei"·"E" 는 단위다
        exponent = int(suffix[1:])
        if abs(exponent) > _MAX_EXPONENT:
            return None
        scale = Fraction(10) ** exponent
    number = math.ceil(Fraction(match[1]) * scale)
    return number if number <= MAX_INT else None
