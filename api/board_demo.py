"""합성 게시판 스레드 — 데모 UI 왼쪽 패널용 데이터.

전형적인 인쇄사 게시판 CS 왕복을 재연한다: 같은 스티커 주문이
게시판에서는 왕복 6회·26시간, 오른쪽 AI 채팅에서는 30초에 끝난다는 대비가 핵심.
타임스탬프와 경과 시간은 그 대비가 체감되도록 반나절~1일 흐름으로 연출했다.

가격(46,200원)은 core/quote/pricebook.yaml 의 스티커 90x90 · art_250 · 500매
+ 무광 코팅 견적(VAT 포함)과 일치시켜 두었다 — 좌우 패널이 같은 주문임을 보이기 위함.
"""

from __future__ import annotations

#: 게시글 1건: seq(순번), author(작성자), role(customer|staff), time(작성 시각),
#: elapsed(직전 글로부터 경과), body(본문), attachment(첨부 파일명, 없으면 None)
BOARD_THREAD: dict = {
    "board": "주문/파일접수 게시판",
    "title": "[주문문의] 사각 스티커 90x90 500매 파일 확인 부탁드립니다",
    "product": "sticker",
    "posts": [
        {
            "seq": 1,
            "author": "김민지",
            "role": "customer",
            "time": "2026-06-29(월) 09:12",
            "elapsed": None,
            "body": (
                "안녕하세요, 카페 로고 스티커 주문하려고 합니다.\n"
                "90x90mm 사이즈로 500매, 재단은 도무송(모양대로)으로 부탁드려요.\n"
                "디자인 파일 첨부했습니다. 견적이랑 제작 가능 여부 확인 부탁드립니다!"
            ),
            "attachment": "cafe_logo_sticker.pdf",
        },
        {
            "seq": 2,
            "author": "애즈랜드 박상우",
            "role": "staff",
            "time": "2026-06-29(월) 11:40",
            "elapsed": "2시간 28분 뒤",
            "body": (
                "안녕하세요 고객님, 문의 감사합니다.\n"
                "첨부해 주신 파일이 미리보기용 저해상도 사본으로 확인됩니다.\n"
                "인쇄용 원본 PDF로 다시 올려주시면 파일 확인 후 안내드리겠습니다."
            ),
            "attachment": None,
        },
        {
            "seq": 3,
            "author": "김민지",
            "role": "customer",
            "time": "2026-06-29(월) 14:03",
            "elapsed": "2시간 23분 뒤",
            "body": (
                "원본 PDF로 다시 올립니다.\n"
                "이 파일로 진행 가능한지 확인 부탁드려요. 다음 주 행사에 써야 해서 조금 급합니다.."
            ),
            "attachment": "cafe_logo_sticker_원본.pdf",
        },
        {
            "seq": 4,
            "author": "애즈랜드 박상우",
            "role": "staff",
            "time": "2026-06-29(월) 16:55",
            "elapsed": "2시간 52분 뒤",
            "body": (
                "파일 확인했습니다. 그런데 재단 여백(사방 3mm)이 없는 상태입니다.\n"
                "이대로 제작하면 재단 시 가장자리가 흰색으로 밀릴 수 있어요.\n"
                "배경을 사방 3mm씩 늘려서 수정 후 다시 올려주시기 바랍니다."
            ),
            "attachment": None,
        },
        {
            "seq": 5,
            "author": "김민지",
            "role": "customer",
            "time": "2026-06-30(화) 09:30",
            "elapsed": "다음날 아침 (16시간 35분 뒤)",
            "body": (
                "퇴근하고 디자인해 준 친구한테 부탁해서 여백 3mm 추가했습니다.\n"
                "수정본 올려요. 이제 문제 없을까요?"
            ),
            "attachment": "cafe_logo_sticker_최종.pdf",
        },
        {
            "seq": 6,
            "author": "애즈랜드 박상우",
            "role": "staff",
            "time": "2026-06-30(화) 11:20",
            "elapsed": "1시간 50분 뒤",
            "body": (
                "수정 파일 이상 없이 확인되었습니다.\n"
                "90x90 아트지 스티커 500매 · 무광 코팅 · 도무송 재단 기준 46,200원(부가세 포함)입니다.\n"
                "결제 확인되는 대로 제작 진행하겠습니다. 감사합니다."
            ),
            "attachment": None,
        },
    ],
    "total_elapsed": "26시간 8분",
    "summary": "게시판 왕복 6회 — 첫 문의부터 주문 확정까지 26시간 8분",
}


def get_board_thread() -> dict:
    """게시판 스레드 반환 (읽기 전용 데이터)."""
    return BOARD_THREAD
