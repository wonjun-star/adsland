"""한글 이름 로마자 변환 + 직위 영문 사전 (영어 병기 명함용).

API 키 없이도 영어 병기가 최소한 동작하도록 규칙 기반 로마자 변환을 둔다.
국립국어원 로마자 표기법(개정)을 이름에 맞게 단순화했다 — 완벽한 표준은 아니고,
LLM(API)이 있으면 더 정확한 영문 표기·회사명 번역을 만든다.
"""

from __future__ import annotations

# 초성 19
_CHO = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
# 중성 21
_JUNG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo",
         "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
# 종성 28 (0=없음)
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "l", "l", "l", "l", "l", "l", "l",
         "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"]

# 흔한 성씨는 관용 표기를 우선 (규칙 변환과 다른 것들)
_SURNAME_FIX = {
    "김": "Kim", "이": "Lee", "박": "Park", "최": "Choi", "정": "Jung", "강": "Kang",
    "조": "Cho", "윤": "Yoon", "장": "Jang", "임": "Lim", "한": "Han", "오": "Oh",
    "서": "Seo", "신": "Shin", "권": "Kwon", "황": "Hwang", "안": "Ahn", "송": "Song",
    "류": "Ryu", "전": "Jeon", "홍": "Hong", "고": "Ko", "문": "Moon", "손": "Son",
    "배": "Bae", "백": "Baek", "허": "Heo", "유": "Yoo", "남": "Nam", "심": "Shim",
    "노": "Noh", "하": "Ha", "곽": "Kwak", "성": "Sung", "차": "Cha", "주": "Joo",
    "우": "Woo", "구": "Koo", "민": "Min",
}


def _romanize_syllable(ch: str) -> str:
    code = ord(ch) - 0xAC00
    if code < 0 or code > 11171:
        return ch
    cho, rem = divmod(code, 588)
    jung, jong = divmod(rem, 28)
    return _CHO[cho] + _JUNG[jung] + _JONG[jong]


def _romanize_block(block: str) -> str:
    return "".join(_romanize_syllable(c) for c in block)


def romanize_name(name: str) -> str:
    """한글 이름 → 로마자. 성 1글자 + 이름으로 보고 'Hwang Wonjun' 형태로.

    성은 관용 표기(_SURNAME_FIX) 우선, 이름은 규칙 변환 후 첫 글자만 대문자.
    한글이 아니면(이미 영문 등) 그대로 돌려준다.
    """
    name = name.strip()
    if not name or not any(0xAC00 <= ord(c) <= 0xD7A3 for c in name):
        return name
    surname = _SURNAME_FIX.get(name[0], _romanize_block(name[0]).capitalize())
    given = _romanize_block(name[1:]).capitalize() if len(name) > 1 else ""
    return f"{surname} {given}".strip()


#: 직위 한국어 → 영문 (병기용). 없으면 영문 생략.
TITLE_EN: dict[str, str] = {
    "대표": "CEO",
    "대표이사": "CEO",
    "사장": "President",
    "부사장": "Vice President",
    "이사": "Director",
    "상무": "Executive Director",
    "전무": "Senior Executive Director",
    "부장": "General Manager",
    "차장": "Deputy General Manager",
    "과장": "Manager",
    "대리": "Assistant Manager",
    "사원": "Staff",
    "팀장": "Team Lead",
    "실장": "Head",
    "본부장": "Division Head",
    "수석연구원": "Senior Researcher",
    "수석 연구원": "Senior Researcher",
    "책임연구원": "Principal Researcher",
    "책임 연구원": "Principal Researcher",
    "선임연구원": "Senior Researcher",
    "선임 연구원": "Senior Researcher",
    "연구원": "Researcher",
    "연구소장": "Head of Research",
    "매니저": "Manager",
    "디자이너": "Designer",
    "개발자": "Developer",
    "엔지니어": "Engineer",
    "기획자": "Planner",
    "컨설턴트": "Consultant",
}


def title_to_en(title: str) -> str:
    """직위 영문. 사전에 있으면 반환, 없으면 빈 문자열(병기 생략)."""
    return TITLE_EN.get(title.strip().replace(" ", ""), TITLE_EN.get(title.strip(), ""))
