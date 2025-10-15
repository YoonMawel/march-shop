# -*- coding: utf-8 -*-
import time, threading, random
from typing import Dict, Tuple, Optional, List
from .config import Config
from .sheets import Sheets

class ShopService:
    """게임 규칙/계산 담당 (상점 캐시, 잔액/체력/아이템, 가챠, 구매한도 등)"""
    def __init__(self, sh: Sheets):
        self.sh = sh
        self._cache: Dict[str, Tuple[int, int, str, str, str, int]] = {}
        # (구매가, 판매가, 설명, 유형, 효과값, 일일한도)
        self._exp  = 0.0
        self._lock = threading.RLock()
        # 필수 행 확보
        self.sh.row_of(Config.CURRENCY)
        self.sh.row_of(Config.HP_NAME)

    # ---- 상점 캐시 ----
    def shop_map(self):
        with self._lock:
            if time.time() > self._exp:
                recs = self.sh.shop.get_all_records()
                mp = {}
                for r in recs:
                    name = str(r.get("아이템명", "")).strip()
                    if not name:
                        continue

                    # 구매가 / 판매가 파싱
                    buy_raw = r.get("구매가")
                    sell_raw = r.get("판매가")

                    try:
                        buy_price = int(buy_raw)
                    except Exception:
                        continue  # 구매가가 없으면 상점 품목으로 취급하지 않음

                    try:
                        sell_price = int(sell_raw) if sell_raw not in (None, "",) else buy_price
                    except Exception:
                        sell_price = max(1, buy_price // 2)

                    desc = str(r.get("설명", "")).strip()
                    typ = str(r.get("유형", "NORMAL")).strip().upper()
                    eff = str(r.get("효과", "")).strip()
                    limit = 0
                    try:
                        limit = int(r.get("일일한도") or 0)
                    except Exception:
                        limit = 0

                    # ▼ 튜플 구성: (구매가, 판매가, 설명, 유형, 효과, 일일한도)
                    mp[name] = (buy_price, sell_price, desc, typ, eff, limit)

                self._cache = mp
                self._exp = time.time() + 600  # 캐시 TTL, 기존 값을 사용하세요
            return self._cache

    # ---- 잔액/아이템/체력 ----
    def balance(self, acct: str) -> int:
        c = self.sh.ensure_user(acct)
        r = self.sh.row_of(Config.CURRENCY)
        return self.sh.read_int(r, c)

    def add_bal(self, acct: str, delta: int):
        c = self.sh.ensure_user(acct)
        r = self.sh.row_of(Config.CURRENCY)
        cur = self.sh.read_int(r, c)
        nv = cur + delta
        if nv < 0:
            raise ValueError("잔액 부족")
        self.sh.write_int(r, c, nv)

    def transfer_bal(self, src: str, dst: str, amount: int):
        if amount <= 0:
            raise ValueError("amount must be positive")
        sc = self.sh.ensure_user(src)
        dc = self.sh.ensure_user(dst)
        row = self.sh.row_of(Config.CURRENCY)
        s_cur = self.sh.read_int(row, sc)
        if s_cur < amount:
            raise ValueError("잔액 부족")
        self.sh.write_int(row, sc, s_cur - amount)
        d_cur = self.sh.read_int(row, dc)
        self.sh.write_int(row, dc, d_cur + amount)

    def add_item(self, acct: str, item: str, qty: int):
        c = self.sh.ensure_user(acct)
        r = self.sh.row_of(item)
        cur = self.sh.read_int(r, c)
        self.sh.write_int(r, c, max(0, cur + qty))

    def remove_item(self, acct: str, item: str, qty: int):
        c = self.sh.ensure_user(acct)
        r = self.sh.row_of(item)
        cur = self.sh.read_int(r, c)
        if cur < qty:
            raise ValueError("아이템 수량 부족")
        self.sh.write_int(r, c, cur - qty)

    # 체력
    def hp(self, acct: str) -> int:
        c = self.sh.ensure_user(acct)
        r = self.sh.row_of(Config.HP_NAME)
        v = self.sh.read_int(r, c)
        return v if v > 0 else Config.HP_MAX  # 미설정이면 최대치부터 시작

    def add_hp(self, acct: str, delta: int) -> int:
        c = self.sh.ensure_user(acct)
        r = self.sh.row_of(Config.HP_NAME)
        cur = self.hp(acct)
        nv = max(0, min(Config.HP_MAX, cur + delta))
        self.sh.write_int(r, c, nv)
        return nv

    # ---- 구매 한도 검사/기록 ----
    def check_daily_limit(self, acct: str, item: str, limit: int, today_prefix: str, extra: int = 1) -> bool:
        """
        오늘 누적 구매량 + 이번 요청량(extra)이 limit 이하여야 True
        """
        bought = self.sh.purchases_today(acct, item, today_prefix)  # 기존 일일 누계
        return (bought + max(1, extra)) <= limit

    def record_purchase(self, acct: str, nick: str, item: str, qty: int, date_ts: str):
        self.sh.purchases_append(acct, nick, date_ts, item, qty)

    # ---- 가챠 엔진 ----
    def gacha_roll(self, table: str):
        rows = self.sh.gacha_table(table)
        if not rows:
            return ("", 0, "아무 일도 일어나지 않았다…")
        weights, results = [], []
        for r in rows:
            item = str(r.get("보상아이템", "")).strip()
            try:
                qty = int(r.get("수량", 1))
            except Exception:
                qty = 1
            try:
                w = float(r.get("확률", 1))
            except Exception:
                w = 1.0
            # '스크립트' 우선, 없으면 '메시지' 백워드 호환
            script = str(r.get("스크립트", r.get("메시지", ""))).strip()
            results.append((item, qty, script))
            weights.append(max(0.0, w))
        if sum(weights) == 0:
            weights = [1.0] * len(results)
        return random.choices(results, weights=weights, k=1)[0]
