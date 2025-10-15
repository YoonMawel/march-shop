# -*- coding: utf-8 -*-
from datetime import datetime, timezone, timedelta

# 고정 KST(UTC+9)
KST = timezone(timedelta(hours=9))

def now_ts():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

def today_str():
    return datetime.now(KST).strftime("%Y-%m-%d")
