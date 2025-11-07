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
                    return self.bot.reply(st, "구매하려는 항목이 비어 있습니다.")

                mp = self.svc.shop_map()
                unknown = [name for name, _ in items if name not in mp]

                if unknown:
                    # 오류 2: 물품 미존재
                    return self.bot.reply(
                        st,
                        "해당 물품은 상점에 존재하지 않습니다. "
                        "오타가 없는지 점검 부탁드리며, "
                        "오기재 · 미등록 등으로 판단될 시 운영 계정(@MARCH)으로 문의해 주십시오.\n\n"
                        f"대상 물품 ― {', '.join(unknown)}"
                    )

                # 일일 한도(아이템별) & 총액 계산
                today = today_str()
                total = 0

                for name, qty in items:
                    buy_price, sell_price, desc, typ, eff, limit = mp[name]
                    if limit and limit > 0:
                        if not self.svc.check_daily_limit(acct, name, limit, today, extra=qty):
                            return self.bot.reply(
                                st,
                                f"'{name}'은(는) 하루 {limit}회/{limit}개까지 구매 가능합니다."
                            )
                    total += buy_price * qty

                bal = self.svc.balance(acct)
                if bal < total:
                    # 오류 1: 화폐 부족
                    return self.bot.reply(
                        st,
                        f"주머니를 털어 보아도 {Config.CURRENCY} {total}개가 보이지 않는다. 다시 확인해 보자.\n\n"
                        f"현재 보유 수량 ― {bal}개"
                    )

                # 결제 1회
                self.svc.add_bal(acct, -total)

                # 지급 + 구매기록
                ts = now_ts()
                for name, qty in items:
                    self.svc.add_item(acct, name, qty)
                    self.svc.record_purchase(acct, nick, name, qty, ts)

                # --- 연출 메시지 ---
                if len(items) == 1:
                    name, qty = items[0]
                    buy_price, sell_price, desc, typ, eff, limit = mp[name]
                    script = desc or ""
                    msg = (
                        f"빈 통에 {Config.CURRENCY}을 넣자 {name} 이/가 나타났다.\n\n"
                        f"― {name}x{qty}\n"
                    )
                    if script:
                        msg += f"{script}\n\n"
                    msg += (
                        f"구매 금액 ― {total} {Config.CURRENCY}\n"
                        f"잔액 ― {bal - total} {Config.CURRENCY}"
                    )
                else:
                    lines = [f"― {n}x{q}" for n, q in items]
                    msg = (
                            f"빈 통에 {Config.CURRENCY}을 넣자 여러 아이템이 나타났다.\n\n"
                            + "\n".join(lines)
                            + "\n\n"
                              f"구매 금액 ― {total} {Config.CURRENCY}\n"
                              f"잔액 ― {bal - total} {Config.CURRENCY}"
                    )

                return self.bot.reply(st, msg)

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
                        # 아이템 or 화폐 획득
                        if g_item == Config.CURRENCY:
                            self.svc.add_bal(acct, g_qty)
                        else:
                            self.svc.add_item(acct, g_item, g_qty)
                        base = f"두근두근, {item} 을/를 사용해 보자⋯.\n\n"
                        if g_script:
                            base += f"{g_script}\n"
                        if g_item == Config.CURRENCY:
                            base += f"획득 ― {Config.CURRENCY} {g_qty}개"
                        else:
                            base += f"획득 ― {g_item}x{g_qty}"
                        return self.bot.reply(st, base)
                    else:
                        # 아이템은 없지만 스크립트만 있을 수도 있음
                        msg = "두근두근, {0} 을/를 사용해 보자⋯.\n\n".format(item)
                        if g_script:
                            msg += g_script
                        else:
                            msg += "⋯아무 일도 일어나지 않았다."
                        return self.bot.reply(st, msg)
                else:
                    return self.bot.reply(st, f"{nick}님, {item} 1개 사용")

            if cmd == "sell":  # 판매
                items = p["items"]

                if not items:
                    return self.bot.reply(st, "판매하려는 항목이 비어 있습니다.")

                mp = self.svc.shop_map()
                unknown = [name for name, _ in items if name not in mp]

                if unknown:
                    # 오류 2: 물품 미존재
                    return self.bot.reply(
                        st,
                        "해당 물품은 상점에 존재하지 않습니다. "
                        "오타가 없는지 점검 부탁드리며, "
                        "오기재 · 미등록 등으로 판단될 시 운영 계정(@MARCH)으로 문의해 주십시오.\n\n"
                        f"대상 물품 ― {', '.join(unknown)}"
                    )

                # 보유량 사전검증
                lack = []
                user_col = self.sh.ensure_user(acct)
                for name, qty in items:
                    row = self.sh.row_of(name)
                    owned = self.sh.read_int(row, user_col)
                    if owned < qty:
                        lack.append(f"{name} x{qty}(보유 {owned})")

                if lack:
                    # 오류 1: 아이템 부족
                    return self.bot.reply(
                        st,
                        "주머니를 털어 보아도 필요한 아이템이 보이지 않는다. 다시 확인해 보자.\n\n"
                        f"현재 보유 수량 ― {', '.join(lack)}"
                    )

                # 판매 금액 계산 (판매가 사용)
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

                if len(items) == 1:
                    name, qty = items[0]
                    msg = (
                        f"빈 통에 {name} 을/를 넣자 {Config.CURRENCY}이 나타났다.\n\n"
                        f"― {name}x{qty}\n\n"
                        f"판매 금액 ― {revenue} {Config.CURRENCY}\n"
                        f"잔액 ― {bal_after} {Config.CURRENCY}"
                    )
                else:
                    lines = [f"― {n}x{q}" for n, q in items]
                    msg = (
                            f"빈 통에 여러 아이템을 넣자 {Config.CURRENCY}이 나타났다.\n\n"
                            + "\n".join(lines)
                            + "\n\n"
                              f"판매 금액 ― {revenue} {Config.CURRENCY}\n"
                              f"잔액 ― {bal_after} {Config.CURRENCY}"
                    )

                return self.bot.reply(st, msg)

            if cmd == "give":
                target = p["target"].strip()
                thing = p["thing"].strip()
                qty = p.get("qty", 1)

                if qty <= 0:
                    return self.bot.reply(st, "양도 수량은 1 이상이어야 합니다.")

                if not self.sh.user_exists(target): #대상 검증
                    return self.bot.reply(
                        st,
                        "양도 대상이 유저 목록에 존재하지 않습니다. 아이디를 다시 확인해 주세요.\n\n"
                        f"양도 대상 ― @{target}"
                    )

                #target 유효성(아이디 포맷 등) 검증은 이미 따로 했다면 그대로 유지

                if thing == Config.CURRENCY:
                    # 화폐 양도
                    try:
                        self.svc.transfer_bal(acct, target, qty)
                    except ValueError:
                        return self.bot.reply(
                            st,
                            f"주머니를 털어 보아도 {Config.CURRENCY} {qty}개가 보이지 않는다. 다시 확인해 보자.\n\n"
                            f"현재 보유 수량 ― {self.svc.balance(acct)}개"
                        )

                    msg = (
                        f"{Config.CURRENCY} {qty}개 양도가 완료되었다.\n\n"
                        f"양도 대상 ― @{target}"
                    )
                    return self.bot.reply(st, msg)

                else:
                    # 아이템 양도
                    try:
                        self.svc.remove_item(acct, thing, qty)
                    except ValueError:
                        return self.bot.reply(
                            st,
                            f"주머니를 털어 보아도 {thing} {qty}개가 보이지 않는다. 다시 확인해 보자.\n\n"
                            "현재 보유 수량은 인벤토리 시트를 확인해 주세요."
                        )

                    self.svc.add_item(target, thing, qty)
                    msg = (
                        f"{thing} {qty}개 양도가 완료되었다.\n\n"
                        f"양도 대상 ― @{target}"
                    )
                    return self.bot.reply(st, msg)

            if cmd == "craft":
                ings = [x.strip() for x in p["ings"]]
                match = self.sh.find_recipe(ings)

                # 재료 필요 수량 집계
                need = Counter(ings)
                user_col = self.sh.ensure_user(acct)

                # 보유량 검증
                lack = []
                for name, q in need.items():
                    row = self.sh.row_of(name)
                    owned = self.sh.read_int(row, user_col)
                    if owned < q:
                        lack.append(f"{name} x{owned}")

                if lack:
                    # 오류 1: 재료 부족
                    return self.bot.reply(
                        st,
                        "주머니를 털어 보아도 필요한 재료가 보이지 않는다. 다시 확인해 보자.\n\n"
                        f"현재 보유 수량 ― {', '.join(lack)}"
                    )

                # 재료는 성공/실패에 관계없이 소모
                for name, q in need.items():
                    self.svc.remove_item(acct, name, q)

                if not match:
                    # 제작 실패
                    msg = (
                        "재료를 한데 넣고 섞어보자. 무엇이 나올까?\n\n"
                        "⋯아무도 보지 않을 때 몰래 버리자.\n"
                        "제작 실패 ― 사용 재료 소모"
                    )
                    return self.bot.reply(st, msg)

                out_item, out_qty = match

                # 결과 지급
                self.svc.add_item(acct, out_item, out_qty)

                # 공개 레시피 기록
                key = Sheets.norm_key(ings)
                self.sh.public_recipe_append(out_item, out_qty, key, acct, nick, today_str())

                msg = (
                    "재료를 한데 넣고 섞어보자. 무엇이 나올까?\n\n"
                    f"{out_item} 이/가 완성되었다!\n"
                    f"제작 성공 ― {out_item}x{out_qty}"
                )
                return self.bot.reply(st, msg)

            if cmd == "job":
                today = today_str()

                if self.sh.job_done_today(acct, today):
                    return self.bot.reply(st, f"{nick}의 아르바이트는 오늘 이미 진행했습니다.")

                reward = random.randint(1, 10)
                self.svc.add_bal(acct, reward)
                self.sh.job_append(acct, nick, today, reward)

                msg = (
                    "노동은 고되나, 본디 남의 주머니에서 돈을 꺼내 가는 건 어려운 일이다.\n\n"
                    f"보상으로 {reward} {Config.CURRENCY}을 받았다!"
                )
                return self.bot.reply(st, msg)


        except Exception as e:
            logging.exception("processing error")
            self.bot.reply(st, f"처리 중 오류: {type(e).__name__}: {e}")

class Listener(StreamListener):
    def __init__(self, disp: 'Dispatch'):
        super().__init__()
        self.disp = disp

    def on_notification(self, notification):
        self.disp.on_notif(notification)
