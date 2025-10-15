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
        """멘션은 유지하고, 유저별 간격에 맞춰 전송을 스케줄링한다."""
        author = status["account"]["acct"]  # "user" 또는 "user@remote"
        now = time.monotonic()
        interval = getattr(Config, "REPLY_INTERVAL_PER_USER", 5)

        with self._cv:
            # 유저별 다음 가능 시각 = max(지금, 마지막예약+간격)
            ready_time = max(now, self._last_sent.get(author, 0.0) + interval)
            self._last_sent[author] = ready_time
            self._seq += 1
            # (ready_time, seq, status, text, author)를 힙에 푸시
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