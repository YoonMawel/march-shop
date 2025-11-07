# -*- coding: utf-8 -*-

class Config:
    # ===== Mastodon (하드코딩) =====
    BASE_URL     = "https://marchen1210d.site/"          # ← 인스턴스 URL
    ACCESS_TOKEN = "94WcWVHVBtcSIEhd34FZCTxlyp30UFz9p4IG6SgsoMc"   # ← 봇 토큰

    # ===== Google Service Account JSON (경로) =====
    CREDS_JSON = "march-credential.json"                 # ← 서비스계정 JSON 경로/파일명

    # ===== 단일 스프레드시트 문서명 =====
    MASTER_SHEET = "상점"        # 문서 1개만 사용

    # ===== 문서 안 워크시트(탭) 이름 =====
    WS_SHOP       = "물품목록"     # 아이템명 | 가격 | 설명 | 유형 | 효과값 | 일일한도
    WS_INV        = "가방"         # 1열=아이템명, 1행=유저 acct(@없음); '코인','체력' 행 포함
    WS_RECIPE     = "레시피"       # 출력아이템 | 출력수량 | 재료키  (비공개)
    WS_JOBS       = "기록"         # 유저 | 날짜 | 지급코인
    WS_PUBLIC_REC = "공개레시피"   # 출력아이템 | 출력수량 | 재료키 | 발견자 | 날짜
    WS_PURCHASE   = "구매기록"     # 유저 | 날짜 | 아이템 | 수량
    WS_GACHA      = "가챠"         # 테이블 | 보상아이템 | 수량 | 확률 | 메시지
    WS_USERS      = "유저목록"

    # ===== 동작 옵션 =====
    REPLY_VIS       = "public"   # public | unlisted | private | direct
    WORKERS         = 8            # 병렬 처리 스레드 수
    SHOP_CACHE_TTL  = 600          # 상점 캐시 TTL(초)

    # 답변 텀
    REPLY_INTERVAL_PER_USER = 15

    # 통화/체력
    CURRENCY        = "갈레온"       # 인벤토리의 통화 행 이름
    HP_NAME         = "체력"
    HP_MAX          = 100          # 기본 최대 체력
