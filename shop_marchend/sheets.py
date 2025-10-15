# -*- coding: utf-8 -*-
import logging, threading, queue, re, time, random
from typing import List, Dict, Optional, Tuple, Iterable
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.utils import rowcol_to_a1
from gspread.exceptions import WorksheetNotFound
from .config import Config

def _a1(r:int,c:int)->str:
    return rowcol_to_a1(r,c)  # Worksheet.update에는 시트명 없이 A1만!

class Sheets:
    """읽기 병렬 OK, 쓰기는 분리 큐(인벤토리/로그)로 직렬·배치 전송."""
    def __init__(self):
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(Config.CREDS_JSON, scope)
        cli = gspread.authorize(creds)

        # 단일 문서
        self.ss = cli.open(Config.MASTER_SHEET)

        # 워크시트들(없으면 생성 + 헤더)
        self.shop = self._get_or_create_ws(Config.WS_SHOP,
            ["아이템명","가격","설명","유형","효과값","일일한도"]) # 물품목록시트
        self.inv  = self._get_or_create_ws(Config.WS_INV, ["아이템명"]) # 가방시트
        self.rec  = self._get_or_create_ws(Config.WS_RECIPE, ["출력아이템","출력수량","재료키"]) # 레시피시트
        self.jobs = self._get_or_create_ws(Config.WS_JOBS, ["유저","닉네임","날짜","지급코인"]) # 아르바이트시트
        self.pubr = self._get_or_create_ws(Config.WS_PUBLIC_REC,
            ["출력아이템","출력수량","재료키","발견자","발견자닉","날짜"]) # 공개레시피시트
        self.purs = self._get_or_create_ws(Config.WS_PURCHASE, ["유저","닉네임","날짜","아이템","수량"]) # 구매기록시트
        self.gacha= self._get_or_create_ws(Config.WS_GACHA, ["테이블","보상아이템","수량","확률","스크립트"]) # 가챠시트
        self.users = self._get_or_create_ws(Config.WS_USERS, ["아이디", "닉네임", "최초활동", "최근활동"]) # 유저목록시트

        # 캐시
        self._hdr: Optional[List[str]] = None
        self._row_cache: Dict[str,int] = {}

        # 쓰기 큐: 인벤토리/로그
        self._wq_inv: queue.Queue = queue.Queue()
        self._wq_log: queue.Queue = queue.Queue()
        threading.Thread(target=self._writer_inv, daemon=True).start()
        threading.Thread(target=self._writer_log, daemon=True).start()

        # 필수 행 보장(통화, 체력)
        self.row_of(Config.CURRENCY)
        self.row_of(Config.HP_NAME)

    # ---- 워크시트 생성/획득 ----
    def _get_or_create_ws(self, title: str, headers: Optional[List[str]] = None):
        try:
            ws = self.ss.worksheet(title)
        except WorksheetNotFound:
            ws = self.ss.add_worksheet(title=title, rows=1000, cols=50)
            if headers:
                ws.update(f"A1:{chr(64+len(headers))}1", [headers])
        return ws

    # ---- 인벤토리 유틸 ----
    def headers(self) -> List[str]:
        if self._hdr is None:
            self._hdr = self.inv.row_values(1)
        return self._hdr

    def ensure_user(self, acct: str) -> int:
        hdr = self.headers()
        if acct not in hdr:
            col = len(hdr) + 1
            self.inv.update_cell(1, col, acct)
            self._hdr = None
            return col
        return hdr.index(acct) + 1

    def row_of(self, item: str) -> int:
        if item in self._row_cache:
            return self._row_cache[item]
        col1 = self.inv.col_values(1)
        for i, v in enumerate(col1, 1):
            if v.strip() == item:
                self._row_cache[item] = i
                return i
        r = len(col1) + 1
        self.inv.update_cell(r, 1, item)
        self._row_cache[item] = r
        return r

    def read_int(self, r: int, c: int) -> int:
        v = self.inv.cell(r, c).value
        try:
            return int(v)
        except Exception:
            return 0

    # ---- 인벤토리 쓰기(시트명 없이 A1) ----
    def write_int(self, r: int, c: int, val: int):
        a1 = _a1(r,c)
        self._wq_inv.put({"range": a1, "values": [[val if val > 0 else ""]]})

    # ---- 배치 drain helpers ----
    def _drain_dict_jobs(self, q:queue.Queue, first, budget_ms:int, max_n:int):
        batch = [first]
        deadline = time.monotonic() + budget_ms/1000.0
        while len(batch) < max_n and time.monotonic() < deadline:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        return batch

    # ---- writers ----
    def _writer_inv(self):
        while True:
            job = self._wq_inv.get()
            if job is None:
                break
            try:
                batch = self._drain_dict_jobs(self._wq_inv, job, 30, 200)
                # 같은 셀은 마지막 값만 남기기
                coalesced: Dict[str, List[List[str]]] = {}
                for j in batch:
                    coalesced[j["range"]] = j["values"]
                data = [{"range": rng, "values": vals} for rng, vals in coalesced.items()]
                try:
                    self.inv.batch_update(data)
                except Exception:
                    for j in data:
                        self.inv.update(j["range"], j["values"])
            except Exception:
                logging.exception("inventory writer failed")
            finally:
                for _ in range(len(batch)):
                    self._wq_inv.task_done()

    def _writer_log(self):
        while True:
            task = self._wq_log.get()
            if task is None:
                break
            try:
                batch = self._drain_dict_jobs(self._wq_log, task, 60, 200)
                # 워크시트별로 묶어서 append_rows
                buckets: Dict[str, List[List[str]]] = {"jobs":[], "purs":[], "pubr":[]}
                for t in batch:
                    buckets[t["ws"]].append(t["row"])
                if buckets["jobs"]:
                    try: self.jobs.append_rows(buckets["jobs"])
                    except Exception:
                        for r in buckets["jobs"]: self.jobs.append_row(r)
                if buckets["purs"]:
                    try: self.purs.append_rows(buckets["purs"])
                    except Exception:
                        for r in buckets["purs"]: self.purs.append_row(r)
                if buckets["pubr"]:
                    try: self.pubr.append_rows(buckets["pubr"])
                    except Exception:
                        for r in buckets["pubr"]: self.pubr.append_row(r)
            except Exception:
                logging.exception("log writer failed")
            finally:
                for _ in range(len(batch)):
                    self._wq_log.task_done()

    # ---- 레시피/공개레시피 ----
    @staticmethod
    def norm_key(parts: list[str]) -> str:
        cleaned = [re.sub(r"\s+","", p.strip().lower()) for p in parts if p.strip()]
        cleaned.sort()
        return "-".join(cleaned)

    def find_recipe(self, ingredients: list[str]) -> Optional[tuple[str,int]]:
        # 입력 재료 정규화
        want = Sheets.norm_key(ingredients)
        for r in self.rec.get_all_records():
            out = str(r.get("출력아이템","")).strip()
            if not out:
                continue
            # 시트의 '재료키'도 정규화 (시트에 순서가 뒤죽박죽이어도 OK)
            raw_key = str(r.get("재료키","")).strip()
            have = Sheets.norm_key(raw_key.split('-')) if raw_key else ""
            if have == want:
                try:
                    qty = int(r.get("출력수량", 1))
                except Exception:
                    qty = 1
                return out, qty
        return None

    def public_recipe_exists(self, key: str) -> bool:
        for r in self.pubr.get_all_records():
            if str(r.get("재료키","")).strip() == key:
                return True
        return False

    def public_recipe_append(self, out_item: str, out_qty: int, key: str, acct: str, nick: str, date: str):
        if not self.public_recipe_exists(key):
            self._wq_log.put({"ws": "pubr", "row": [out_item, out_qty, key, acct, nick, date]})

    # ---- 아르바이트 기록 ----
    def job_done_today(self, acct: str, today: str) -> bool:
        for r in self.jobs.get_all_records():
            if str(r.get("유저","")).strip() == acct and str(r.get("날짜","")).strip() == today:
                return True
        return False

    def job_append(self, acct: str, nick: str, date: str, reward: int):
        self._wq_log.put({"ws": "jobs", "row": [acct, nick, date, reward]})

    # ---- 구매 한도/기록 ----
    def purchases_today(self, acct: str, item: str, date_prefix: str) -> int:
        total = 0
        for r in self.purs.get_all_records():
            if (str(r.get("유저", "")).strip() == acct and
                    str(r.get("아이템", "")).strip() == item):
                ts = str(r.get("날짜", "")).strip()  # 예: '2025-09-22 07:41:03'
                if ts.startswith(date_prefix):  # '2025-09-22' 접두어 비교
                    try:
                        total += int(r.get("수량", 0))
                    except Exception:
                        pass
        return total

    def purchases_append(self, acct: str, nick: str, date_ts: str, item: str, qty: int):
        self._wq_log.put({"ws": "purs", "row": [acct, nick, date_ts, item, qty]})

    # ---- 가챠 테이블 ----
    def gacha_table(self, table_name: str) -> List[Dict]:
        rows = []
        for r in self.gacha.get_all_records():
            if str(r.get("테이블", "")).strip() == table_name:
                rows.append(r)  # 스크립트/메시지는 service에서 처리(폴백)
        return rows

    def upsert_user(self, acct: str, nick: str, ts: str):
        if not hasattr(self, "_user_row"):
            self._user_row = {}
        # 캐시에 있으면 최근활동/닉네임만 갱신
        if acct in self._user_row:
            r = self._user_row[acct]
            self.users.update(f"B{r}:D{r}", [[nick, "", ts]])
            return
        # 시트 스캔(최초 1회)
        for i, rec in enumerate(self.users.get_all_records(), start=2):
            if str(rec.get("아이디", "")).strip() == acct:
                self._user_row[acct] = i
                self.users.update(f"B{i}:D{i}", [[nick, "", ts]])
                return
        # 신규
        self.users.append_row([acct, nick, ts, ts])
        self._user_row[acct] = len(self.users.get_all_values())