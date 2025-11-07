# -*- coding: utf-8 -*-
import logging, time, threading, heapq
from mastodon import Mastodon
from .config import Config

class Bot:
    def __init__(self):
        self.api = Mastodon(
            api_base_url=Config.BASE_URL,
            access_token=Config.ACCESS_TOKEN,
            ratelimit_method="pace",
        )
        me = self.api.account_verify_credentials()
        self.me_acct = me["acct"]
        logging.info(f"Bot login @{self.me_acct}")

        # ▶ 유저별 페이싱 상태
        self._last_sent = {}   # acct -> last ready_time (monotonic)
        self._pq = []          # min-heap of (ready_time, seq, status, text, author)
        self._cv = threading.Condition()
        self._seq = 0

        # 전송 워커 시작
        t = threading.Thread(target=self._sender, daemon=True)
        t.start()

    def reply(self, status: dict, text: str):
        """멘션은 유지하고, 유저별 고정 지연 후 전송을 스케줄링한다."""
        author = status["account"]["acct"]
        now = time.monotonic()
        interval = getattr(Config, "REPLY_INTERVAL_PER_USER", 5)

        ready_time = now + interval  #항상 지금으로부터 interval초 뒤

        with self._cv:
            self._seq += 1
            heapq.heappush(self._pq, (ready_time, self._seq, status, text, author))
            self._cv.notify()

    def _sender(self):
        while True:
            with self._cv:
                while not self._pq:
                    self._cv.wait()
                ready, seq, status, text, author = heapq.heappop(self._pq)
                wait = ready - time.monotonic()
            if wait > 0:
                time.sleep(wait)

            try:
                body = f"@{author} {text}"  # 알림용 멘션은 그대로 유지
                self.api.status_post(
                    status=body,
                    in_reply_to_id=status["id"],
                    visibility=Config.REPLY_VIS,
                )
            except Exception:
                logging.exception("reply send failed")