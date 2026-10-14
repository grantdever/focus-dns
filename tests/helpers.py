import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from focus.actions import Focus
from focus.config import Config
from focus import core
from focus.state import DEFAULT_HOURS, default_state

from .fake import FakeApi

NY = ZoneInfo("America/New_York")


def T(y, m, d, h=0, mi=0) -> float:
    return dt.datetime(y, m, d, h, mi, tzinfo=NY).timestamp()


TUE_NOON = T(2026, 10, 13, 12, 0)     # weekday, inside the 00:00-17:00 block
TUE_EVENING = T(2026, 10, 13, 18, 0)  # weekday, open
SAT_NOON = T(2026, 10, 17, 12, 0)     # weekend, open


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class AppCase(unittest.TestCase):
    start = TUE_NOON

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(state_dir=self.tmp / "state", env_file=self.tmp / "none.env", token_file=self.tmp / "token",
                          unit_dir=self.tmp / "units", systemd=False, tailnet_ip="100.64.0.1")
        self.api = FakeApi()
        self.clock = Clock(self.start)
        self.calls = []
        self.app = Focus(self.cfg, api=self.api, runner=lambda c: (self.calls.append(c), (0, ""))[1], clock=self.clock)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def st(self):
        return self.app.state()


def fresh(**over) -> dict:
    st = default_state()
    st.update(over)
    return st


def week(**over) -> dict:
    """A {timezone, hours} week (the shape the schedule functions take), the default hours updated."""
    h = dict(DEFAULT_HOURS)
    h.update(over)
    return {"timezone": "America/New_York", "hours": h}


def lst(st: dict, name: str) -> dict:
    return core.get_list(st, name)
