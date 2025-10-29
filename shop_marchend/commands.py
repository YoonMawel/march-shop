# -*- coding: utf-8 -*-
import re, time, random, logging
import html
from concurrent.futures import ThreadPoolExecutor
from typing import Dict
from mastodon import StreamListener
from collections import Counter

from .config import Config
from .masto import Bot
from .sheets import Sheets
from .service import ShopService
from .utils_time import now_ts, today_str

_ITEM_TOKEN = re.compile(r"^\s*(.+?)(?:\s*[x\*]\s*(\d+))?\s*$")

class Parser:
    # [양도/상대닉(or아이디):아이템명:개수]
    RE_BUY    = re.compile(r"\[\s*구매\s*/\s*([^\]]+)\]")
    RE_USE    = re.compile(r"\[\s*사용\s*/\s*([^\]]+)\]")
    RE_SELL   = re.compile(r"\[\s*판매\s*/\s*([^\]]+)\]")
    RE_GIVE   = re.compile(r"\[\s*양도\s*/\s*([^:\]]+)\s*:\s*([^:\]]+)\s*:\s*(\d+)\s*\]")
    RE_CRAFT  = re.compile(r"\[\s*제작\s*/\s*([^\]]+)\]")
    RE_JOB    = re.compile(r"\[\s*아르바이트\s*\]")
    RE_STATUS = re.compile(r"\[\s*상태\s*\]")  # 추가: 상태 보기

    @staticmethod
    def parse_item_list(s: str):
        """
        '아이템*3-다른아이템x2-세번째' → [("아이템",3),("다른아이템",2),("세번째",1)]
        공백 허용, 구분자는 하이픈('-')
        """
        parts = [p for p in (s or "").split("-") if p.strip()]
        out = []
        for p in parts:
            m = _ITEM_TOKEN.match(p)
            if not m:
                continue
            name = m.group(1).strip()
            qty = int(m.group(2)) if m.group(2) else 1
            if qty <= 0:
                qty = 1
            out.append((name, qty))
        return out

    @staticmethod
    def clean_html(t: str) -> str:
        return re.sub(r"<[^>]+>", " ", t).strip()

    def has_command(self, t: str) -> bool:
        return any([
            self.RE_BUY.search(t),
            self.RE_USE.search(t),
            self.RE_SELL.search(t),
            self.RE_GIVE.search(t),
            self.RE_CRAFT.search(t),
            self.RE_JOB.search(t),
            self.RE_STATUS.search(t),
        ])

    def parse(self, t: str):
        m = self.RE_BUY.search(t)

        if m:
            return {"cmd": "buy", "items": Parser.parse_item_list(m.group(1))}

        m = self.RE_USE.search(t)

        if m:
            return {"cmd": "use", "item": m.group(1).strip()}

        m = self.RE_SELL.search(t)

        if m:
            return {"cmd": "sell", "items": Parser.parse_item_list(m.group(1))}

        m = self.RE_GIVE.search(t)

        if m:
            return {"cmd": "give", "target": m.group(1).strip(),
                    "thing": m.group(2).strip(), "qty": int(m.group(3))}

        m = self.RE_CRAFT.search(t)

        if m:
            parts = [p.strip() for p in m.group(1).split('-') if p.strip()]
            return {"cmd": "craft", "ings": parts}

        if self.RE_JOB.search(t):
            return {"cmd": "job"}

        if self.RE_STATUS.search(t):
            return {"cmd": "status"}

        return {"cmd": "unknown"}


class Dispatch:
    """멘션 병렬 처리(스레드풀). 시트 쓰기는 Sheets 내부 분리 큐로 직렬/배치."""
    def __init__(self, bot: Bot, svc: ShopService, sh: Sheets):
        self.bot = bot
        self.svc = svc
        self.sh  = sh
        self.parser = Parser()
        self.exec = ThreadPoolExecutor(max_workers=Config.WORKERS)

    def _nick_from_status(self, st):
        dn = st.get("account", {}).get("display_name") or ""
        dn = re.sub(r"<[^>]+>", "", dn)
        return html.unescape(dn).strip() or st.get("account", {}).get("acct", "")

    def on_notif(self, notif: dict):
        if notif.get("type") != "mention":
            return

        st = notif.get("status")

        if not st:
            return

        acct = notif["account"]["acct"]  # @ 없이
        txt  = self.parser.clean_html(st["content"])

        # 명령 형식이 아니면 조용히 무시
        if not self.parser.has_command(txt):
            return

        self.exec.submit(self._proc, st, acct, txt)

    def _proc(self, st: dict, acct: str, text: str):
        try:
            p = self.parser.parse(text)
            cmd = p["cmd"]

            # 닉네임 확보
            nick = self._nick_from_status(st)
            ts = now_ts()  # KST 고정
            self.sh.upsert_user(acct, nick, ts)

            if cmd == "unknown": #인식할 수 있는 명령이 아니면 무시
                return 

            if cmd == "status":
                bal = self.svc.balance(acct)
                hp = self.svc.hp(acct)
                return self.bot.reply(st,f"{nick}님의 상태 — {Config.CURRENCY}: {bal}, {Config.HP_NAME}: {hp}/{Config.HP_MAX}")

            if cmd == "buy":
                items = p["items"]  # [("아이템명",수량), ...]

                if not items:
                    return self.bot.reply(st, f"구매하려는 항목이 비어 있습니다.")

                mp = self.svc.shop_map()
                unknown = [name for name, _ in items if name not in mp]

                if unknown:
                    return self.bot.reply(st, f"해당 아이템은 상점에 등록되어 있지 않습니다. 총괄계에 별도 문의 부탁드립니다. : {', '.join(unknown)}")

                # 일일 한도(아이템별) & 총액 계산 (KST)
                today = today_str()
                total = 0

                for name, qty in items:
                    buy_price, sell_price, desc, typ, eff, limit = mp[name]
                    if limit and limit > 0:
                        # 이미 오늘 산 개수
                        if not self.svc.check_daily_limit(acct, name, limit, today, extra=qty):
                            return self.bot.reply(
                                st, f"'{name}'은(는) 하루 {limit}회/{limit}개까지 구매 가능합니다."
                            )
                    total += buy_price * qty

                bal = self.svc.balance(acct)

                if bal < total:
                    return self.bot.reply(st, f"{nick}의 {Config.CURRENCY}이/가 부족합니다. (필요 {total}, 보유 {bal})")

                # 결제 1회
                self.svc.add_bal(acct, -total)

                # 지급 + 구매기록(아이템별) 남기기
                ts = now_ts()
                for name, qty in items:
                    self.svc.add_item(acct, name, qty)
                    # 기록은 아이템별로 한 줄씩
                    self.svc.record_purchase(acct, nick, name, qty, ts)

                # 요약 메시지
                lines = [f"- {n} x{q}" for n, q in items]

                return self.bot.reply(
                    st,
                    f"{nick}이/가 아이템을 구매하였습니다.\n" +
                    "\n".join(lines) +
                    f"\n총액 {total} {Config.CURRENCY}, 잔액 {bal - total} {Config.CURRENCY}"
                )

            if cmd == "use":
                item = p["item"]
                mp = self.svc.shop_map()
                meta = mp.get(item)

                if meta:
                    buy_price, sell_price, desc, typ, eff, limit = meta
                else:
                    typ, eff = "NORMAL", ""

                try:
                    self.svc.remove_item(acct, item, 1)
                except ValueError:
                    return self.bot.reply(st, f"{nick}이 보유중인 아이템 수량이 부족합니다.")

                if typ == "HEAL":
                    try:
                        heal = int(eff) if eff else 0
                    except Exception:
                        heal = 0

                    new_hp = self.svc.add_hp(acct, heal)

                    return self.bot.reply(st,
                                          f"{nick}, {item} 사용 → {Config.HP_NAME} +{heal} (현재 {new_hp}/{Config.HP_MAX})")

                elif typ == "GACHA":
                    table = eff or item
                    g_item, g_qty, g_script = self.svc.gacha_roll(table)

                    if g_item:
                        if g_item == Config.CURRENCY:
                            self.svc.add_bal(acct, g_qty)
                            msg = f"{nick}이/가 {item}을 사용합니다. 획득: +{g_qty} {Config.CURRENCY}"
                        else:
                            self.svc.add_item(acct, g_item, g_qty)
                            msg = f"{nick}이/가 {item}을 사용합니다. 획득: {g_item} x{g_qty}"

                        if g_script:
                            msg += f"\n{g_script}"
                        return self.bot.reply(st, msg)

                    else:
                        return self.bot.reply(st, g_script or f"{nick}이/가 {item}을(를) 사용했지만 아무 일도 일어나지 않았습니다…")

                else:
                    return self.bot.reply(st, f"{nick}님, {item} 1개 사용")

            if cmd == "sell": # 판매
                items = p["items"]

                if not items:
                    return self.bot.reply(st, f"판매하려는 항목이 비어 있습니다.")

                mp = self.svc.shop_map()
                unknown = [name for name, _ in items if name not in mp]

                if unknown:
                    return self.bot.reply(st, f"해당 아이템은 상점에 등록되어 있지 않습니다. 총괄계에 별도 문의 부탁드립니다. : {', '.join(unknown)}")

                # 보유량 사전검증
                lack = []
                user_col = self.sh.ensure_user(acct)

                for name, qty in items:
                    row = self.sh.row_of(name)
                    owned = self.sh.read_int(row, user_col)
                    if owned < qty:
                        lack.append(f"{name} x{qty}(보유 {owned})")
                if lack:
                    return self.bot.reply(st, f"{nick}의 아이템 수량 부족: " + ", ".join(lack))

                # 판매 단가: 시트 '판매가' 열 사용 (비어 있으면 service에서 구매가로 기본 처리)
                revenue = 0
                for name, qty in items:
                    buy_price, sell_price, *_ = mp[name]
                    revenue += sell_price * qty

                bal_before = self.svc.balance(acct)

                # 회수 → 일괄 입금
                for name, qty in items:
                    self.svc.remove_item(acct, name, qty)
                self.svc.add_bal(acct, revenue)

                bal_after = bal_before + revenue
                lines = [f"- {n} x{q}" for n, q in items]

                return self.bot.reply(
                    st,
                    f"{nick}이/가 아이템을 판매하였습니다.\n" +
                    "\n".join(lines) +
                    f"\n판매액 +{revenue} {Config.CURRENCY}, 현재 {bal_after} 보유중"
                )

            if cmd == "give":
                target = p["target"];
                thing = p["thing"];
                qty = p.get("qty", 1)

                if qty <= 0:
                    return self.bot.reply(st, f"양도 수량은 1 이상이어야 합니다.")

                if thing == Config.CURRENCY:
                    try:
                        self.svc.transfer_bal(acct, target, qty)
                    except ValueError:
                        return self.bot.reply(st, f"{nick}의 {Config.CURRENCY}가 부족합니다.")
                    return self.bot.reply(st, f"{nick}이/가 @{target}에게 {Config.CURRENCY} {qty}개를 양도했습니다.")

                else:
                    try:
                        self.svc.remove_item(acct, thing, qty)

                    except ValueError:
                        return self.bot.reply(st, f"{nick}의 보유 수량 부족: {thing} x{qty}")

                    self.svc.add_item(target, thing, qty)
                    return self.bot.reply(st, f"{nick}이/가 @{target}에게 {thing} x{qty}를 양도했습니다.")

            if cmd == "craft":
                ings = [x.strip() for x in p["ings"]]
                # 재료 필요 수량 집계 (같은 재료 중복 입력 허용)
                need = Counter(ings)

                # 1) 보유량 사전 검증 (부족하면 아무 것도 소모되지 않음)
                user_col = self.sh.ensure_user(acct)
                for name, q in need.items():
                    row = self.sh.row_of(name)
                    owned = self.sh.read_int(row, user_col)
                    if owned < q:
                        return self.bot.reply(
                            st,
                            f"{nick}이/가 보유한 재료가 부족합니다: {name} x{q} (보유 {owned})"
                        )

                # 2) 충분하면 재료를 '항상' 차감
                for name, q in need.items():
                    self.svc.remove_item(acct, name, q)

                # 3) 레시피 매칭 → 성공 시 결과 지급 + 공개레시피(선택) 기록
                match = self.sh.find_recipe(ings)
                if match:
                    out_item, out_qty = match
                    self.svc.add_item(acct, out_item, out_qty)

                    # 공개 레시피 자동 기재 유지 (성공시에만)
                    key = Sheets.norm_key(ings)
                    self.sh.public_recipe_append(
                        out_item, out_qty, key, acct, nick, today_str()
                    )

                    return self.bot.reply(
                        st, f"{nick}이/가 제작을 완료했습니다. → {out_item} x{out_qty}"
                    )
                else:
                    # 매칭 실패: 재료는 이미 소모됨
                    return self.bot.reply(
                        st, f"레시피가 없습니다. 입력한 재료는 소모되었습니다."
                    )

            if cmd == "job":
                today = today_str()

                if self.sh.job_done_today(acct, today):
                    return self.bot.reply(st, f"{nick}의 아르바이트는 오늘 이미 진행했습니다.")

                reward = random.randint(1, 10)
                self.svc.add_bal(acct, reward)
                self.sh.job_append(acct, nick, today, reward)

                return self.bot.reply(st, f"{nick}의 아르바이트가 완료되었습니다. +{reward} {Config.CURRENCY}")

        except Exception as e:
            logging.exception("processing error")
            self.bot.reply(st, f"처리 중 오류: {type(e).__name__}: {e}")

class Listener(StreamListener):
    def __init__(self, disp: 'Dispatch'):
        super().__init__()
        self.disp = disp

    def on_notification(self, notification):
        self.disp.on_notif(notification)
