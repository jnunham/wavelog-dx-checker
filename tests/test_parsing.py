"""Parsing tests on ARRL DX Bulletin layouts.  Run:  python3 -m unittest discover tests"""
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dxcheck as d  # noqa: E402

TODAY = dt.date(2026, 11, 13)


def stations(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        text = f.read()
    calls = d.extract_calls(text)
    ref = d.bulletin_date(text)
    out = {}
    for c, v in calls.items():
        if not v["manager"]:
            s, e = d.activity_window(v["context"], ref)
            out[c] = (d.window_label(s, e, TODAY), v["context"])
    return out


class Layouts(unittest.TestCase):
    PREFIX_FIRST = {"KC4USV": "", "KC4/KL5SE": "", "D44JB": "until Nov 20", "VY0ERC": "until Dec 10"}

    def check(self, name, expected):
        got = stations(name)
        self.assertEqual(set(got), set(expected), "wrong set of DX stations")
        for call, when in expected.items():
            self.assertEqual(got[call][0], when, f"dates for {call}")
            # each write-up mentions only its own station(s)
            for other in expected:
                if other.split("/")[0][:3] != call.split("/")[0][:3]:
                    self.assertNotIn(other, got[call][1], f"{call}'s text includes {other}")

    def test_prefix_first_with_blank_lines(self):
        self.check("a_prefix_first_blank.txt", self.PREFIX_FIRST)

    def test_prefix_first_run_together(self):
        # The emailed bulletin often has no blank lines between entries.
        self.check("b_prefix_first_runon.txt", self.PREFIX_FIRST)

    def test_country_first(self):
        self.check("c_country_first.txt", {"4U1ITU": "Nov 14–16", "5X2Z": "",
                                           "9Z4Y": "until Nov 30", "AO100IARU": "ended Nov 1"})

    def test_credits_and_contests_skipped(self):
        for name in ("a_prefix_first_blank.txt", "b_prefix_first_runon.txt", "c_country_first.txt"):
            got = stations(name)
            for not_dx in ("W1AW", "WA7BNM", "K1USN", "KL5SE", "EI8JB", "VE3KTN", "KF5NSR", "W3HNK"):
                self.assertNotIn(not_dx, got, f"{not_dx} in {name}")


class Dates(unittest.TestCase):
    CASES = {
        "QRV as 5R8XX from November 14 to 28.": "Nov 14–28",
        "QRV as KC4USV from McMurdo Station until February.": "until Feb 28, 2027",
        "QRV as A35RK until November 30 on 20m.": "until Nov 30",
        "QRV as 3D2AG from November 1 to 7.": "ended Nov 7",
        "active this weekend at 1400Z on 14.025 MHz.": "",
        "QRV from November 28 to December 6, including CQ WW.": "Nov 28–Dec 6",
        "QRV from January 10 to 24, 2027 as 3D2R.": "Jan 10–24, 2027",
        "Look for V31XX on November 15 and 16 on 20 meters.": "Nov 15–16",
        "TZ4AM is QRV until the end of the year.": "",
        "A52XX will be QRV from December 28 to January 5.": "Dec 28–Jan 5, 2027",
    }

    def test_dates(self):
        for text, want in self.CASES.items():
            s, e = d.activity_window(text, dt.date(2026, 11, 12))
            self.assertEqual(d.window_label(s, e, TODAY), want, text)


if __name__ == "__main__":
    unittest.main()
