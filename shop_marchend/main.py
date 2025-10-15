# -*- coding: utf-8 -*-
import time, logging
from .masto import Bot
from .sheets import Sheets
from .service import ShopService
from .commands import Dispatch, Listener

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot()
    sh  = Sheets()
    svc = ShopService(sh)
    disp = Dispatch(bot, svc, sh)

    logging.info("stream start")
    while True:
        try:
            bot.api.stream_user(Listener(disp), run_async=False, reconnect_async=False)
        except Exception:
            logging.exception("stream error; retry in 5s")
            time.sleep(5)
