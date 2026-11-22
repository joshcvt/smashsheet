#!/usr/bin/env python3
"""smashsheet: copy recent Smashrun runs into a running-log Google Sheet.

For every Smashrun run that started on or after max(start_date, now -
lookback_hours), check whether the sheet already has a logged row for that
date; if not, fill in the next blank row. Optionally guess whether the run was
your "standard run" (a regular route) and tag it.

Usage:
    smashsheet.py [--config PATH] [--dry-run] [-v]
"""
import argparse
import datetime as dt
import json
import logging
import math
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore", module="urllib3")
warnings.filterwarnings("ignore", category=FutureWarning, module="google")

import requests  # noqa: E402
from google.auth.transport.requests import AuthorizedSession  # noqa: E402
from google.oauth2 import service_account  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SMASHRUN_API = "https://api.smashrun.com/v1"
SMASHRUN_TOKEN_URL = "https://secure.smashrun.com/oauth2/token"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
SHEETS_EPOCH = dt.date(1899, 12, 30)

DEFAULT_COLUMNS = {"date": "A", "run_time": "B", "km": "C"}
DEFAULT_RATE_LIMIT = {"min_interval_s": 5, "max_calls": 20, "window_s": 600}

log = logging.getLogger("smashsheet")


class AuthError(Exception):
    pass


# ---------------------------------------------------------------- config

def resolve(path):
    return path if os.path.isabs(path) else os.path.join(HERE, path)


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg["_path"] = path
    return cfg


def save_config(cfg):
    path = cfg["_path"]
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """At most one call per min_interval_s and max_calls per window_s.

    Call times persist in a state file so separate invocations share the budget.
    """

    def __init__(self, state_file, min_interval_s, max_calls, window_s):
        self.state_file = state_file
        self.min_interval_s = min_interval_s
        self.max_calls = max_calls
        self.window_s = window_s
        try:
            with open(state_file) as f:
                self.calls = json.load(f)
        except (OSError, ValueError):
            self.calls = []

    def wait(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < self.window_s]
        delay = 0.0
        if self.calls:
            delay = self.calls[-1] + self.min_interval_s - now
        if len(self.calls) >= self.max_calls:
            delay = max(delay, self.calls[-self.max_calls] + self.window_s - now)
        if delay > 0:
            log.debug("Rate limit: sleeping %.1fs", delay)
            time.sleep(delay)
        self.calls.append(time.time())
        with open(self.state_file, "w") as f:
            json.dump(self.calls, f)


# ---------------------------------------------------------------- smashrun

class Smashrun:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sr = cfg["smashrun"]
        self.http = requests.Session()
        rl = dict(DEFAULT_RATE_LIMIT, **self.sr.get("rate_limit", {}))
        self.limiter = RateLimiter(resolve(".smashrun_calls.json"), **rl)
        self._details = {}

    def _refresh(self):
        sr = self.sr
        if not (sr.get("client_id") and sr.get("refresh_token")):
            return False
        log.info("Refreshing Smashrun access token")
        self.limiter.wait()
        r = requests.post(SMASHRUN_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": sr["refresh_token"],
            "client_id": sr["client_id"],
            "client_secret": sr.get("client_secret", ""),
        }, timeout=30)
        if r.status_code != 200:
            log.error("Smashrun token refresh failed: %s %s", r.status_code, r.text[:300])
            return False
        tok = r.json()
        sr["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            sr["refresh_token"] = tok["refresh_token"]
        save_config(self.cfg)
        return True

    def get(self, path, **params):
        for attempt in (1, 2):
            self.limiter.wait()
            log.debug("GET %s %s", path, params)
            r = self.http.get(
                SMASHRUN_API + path, params=params, timeout=30,
                headers={"Authorization": "Bearer " + self.sr["access_token"]})
            if r.status_code == 401 and attempt == 1 and self._refresh():
                continue
            if r.status_code == 401:
                raise AuthError(
                    "Smashrun rejected the access token (expired?). Get a new one "
                    "from https://api.smashrun.com/v1/explorer (client id 'client') "
                    "and put it in smashrun.access_token in the config.")
            r.raise_for_status()
            return r.json()

    def recent_runs(self, count=20):
        return self.get("/my/activities/search", count=count)

    def run_detail(self, activity_id):
        if activity_id not in self._details:
            self._details[activity_id] = self.get("/my/activities/%s" % activity_id)
        return self._details[activity_id]


def run_start_local(run):
    # e.g. "2026-11-18T07:02:11-05:00", sometimes with fractional seconds
    s = run["startDateTimeLocal"]
    if "." in s:
        s = s[:19] + s[-6:]
    return dt.datetime.fromisoformat(s)


def first_gps_point(detail):
    keys = detail.get("recordingKeys") or []
    vals = detail.get("recordingValues") or []
    if "latitude" not in keys or "longitude" not in keys:
        return None
    lats = vals[keys.index("latitude")]
    lons = vals[keys.index("longitude")]
    for lat, lon in zip(lats, lons):
        if lat and lon:
            return lat, lon
    return None


def haversine_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def matches_standard_run(run, smashrun, std):
    """Cheap guess: distance in band and (if configured) GPS start nearby."""
    km = float(run["distance"])
    if not (std["min_km"] <= km <= std["max_km"]):
        return False, "distance %.2f km outside %.2f-%.2f" % (km, std["min_km"], std["max_km"])
    if std.get("start_lat") is None:
        return True, "distance in band (no start point configured)"
    if run.get("isTreadmill") or run.get("hasDetailsGPS") is False:
        return False, "no GPS"
    pt = first_gps_point(smashrun.run_detail(run["activityId"]))
    if pt is None:
        return False, "no GPS"
    d = haversine_m(pt, (std["start_lat"], std["start_lon"]))
    if d > std.get("start_radius_m", 250):
        return False, "started %.0f m from standard start" % d
    return True, "distance in band, started %.0f m from standard start" % d


# ---------------------------------------------------------------- sheet

def col_index(letter):
    n = 0
    for ch in letter.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


class Sheet:
    def __init__(self, cfg):
        g = cfg["google"]
        creds = service_account.Credentials.from_service_account_file(
            resolve(g["service_account_file"]), scopes=SHEETS_SCOPE)
        self.http = AuthorizedSession(creds)
        self.id = g["spreadsheet_id"]
        self.tab = g.get("sheet_name", "Sheet1")
        self.layout = cfg.get("sheet_layout", {})
        self.cols = dict(DEFAULT_COLUMNS, **self.layout.get("columns", {}))
        self.copy_cols = self.layout.get("copy_from_previous_row", [])
        self.formulas = self.layout.get("row_formulas", {})

    def read_rows(self):
        """Rows 2+ as dicts: row, date, run_time, plus each copy_from_previous_row column."""
        r = self.http.get(
            "%s/%s/values/%s!A2:ZZ" % (SHEETS_API, self.id, self.tab),
            params={"valueRenderOption": "UNFORMATTED_VALUE",
                    "dateTimeRenderOption": "SERIAL_NUMBER"})
        r.raise_for_status()
        rows = []
        for i, vals in enumerate(r.json().get("values", [])):
            def cell(letter):
                j = col_index(letter)
                return vals[j] if j < len(vals) else ""
            a = cell(self.cols["date"])
            rows.append({
                "row": i + 2,
                "date": SHEETS_EPOCH + dt.timedelta(days=int(a)) if isinstance(a, (int, float)) else None,
                "run_time": cell(self.cols["run_time"]),
                "copy": {c: cell(c) for c in self.copy_cols},
            })
        return rows

    def write_run(self, target, prev, delta_days, run_time, km, extra):
        c = self.cols
        t = target
        values = {
            # chain the date off the previous row so pre-filled future dates follow
            c["date"]: "=%s%d+%d" % (c["date"], prev["row"], delta_days),
            c["run_time"]: run_time,
            c["km"]: km,
        }
        values.update(prev["copy"])
        values.update({col: f.replace("{r}", str(t)) for col, f in self.formulas.items()})
        values.update(extra)
        data = [{"range": "%s!%s%d" % (self.tab, col, t), "values": [[v]]}
                for col, v in values.items()]
        r = self.http.post("%s/%s/values:batchUpdate" % (SHEETS_API, self.id),
                           json={"valueInputOption": "USER_ENTERED", "data": data})
        r.raise_for_status()


# ---------------------------------------------------------------- main

def fmt_duration(seconds):
    seconds = int(round(seconds))
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def sync(cfg, dry_run=False):
    start_date = dt.date.fromisoformat(cfg["start_date"])
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cfg["lookback_hours"])
    std = cfg.get("standard_run")
    if std and not std.get("enabled", True):
        std = None
    tread = cfg.get("treadmill")
    if tread and not tread.get("enabled", True):
        tread = None

    smashrun = Smashrun(cfg)
    candidates = []
    for run in smashrun.recent_runs():
        started = run_start_local(run)
        if started < cutoff or started.date() < start_date:
            continue
        candidates.append((started, run))
    candidates.sort(key=lambda c: c[0])
    log.info("%d Smashrun run(s) since %s (start_date %s, lookback %sh)",
             len(candidates), cutoff.astimezone().isoformat(timespec="minutes"),
             start_date, cfg["lookback_hours"])
    if not candidates:
        return 0

    sheet = Sheet(cfg)
    rows = sheet.read_rows()
    added = 0
    for started, run in candidates:
        day = started.date()
        filled = [r for r in rows if r["run_time"] not in ("", None)]
        if any(r["date"] == day for r in filled):
            log.info("%s already in sheet", day)
            continue
        prev = filled[-1]
        if prev["date"] and day < prev["date"]:
            log.warning("%s is older than the last logged row (%s); not inserting mid-sheet",
                        day, prev["date"])
            continue
        target = prev["row"] + 1
        km = round(float(run["distance"]), 2)
        run_time = fmt_duration(run["duration"])
        extra, note = {}, ""
        if tread and run.get("isTreadmill"):
            note = ", treadmill"
            extra = tread.get("set_columns", {})
        elif std:
            hit, why = matches_standard_run(run, smashrun, std)
            note = ", %s=%s (%s)" % (std.get("label", "standard run"), hit, why)
            if hit:
                extra = std.get("set_columns", {})
        log.info("%s %s: %s, %.2f km%s -> row %d",
                 "Would add" if dry_run else "Adding", day, run_time, km, note, target)
        if not dry_run:
            sheet.write_run(target, prev, (day - prev["date"]).days, run_time, km, extra)
        rows = [r for r in rows if r["row"] != target] + [
            {"row": target, "date": day, "run_time": run_time, "copy": prev["copy"]}]
        rows.sort(key=lambda r: r["row"])
        added += 1
    return added


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="don't write to the sheet")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    try:
        n = sync(cfg, dry_run=args.dry_run)
    except AuthError as e:
        log.error("%s", e)
        return 2
    log.info("Done; %d run(s) %s", n, "would be added" if args.dry_run else "added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
