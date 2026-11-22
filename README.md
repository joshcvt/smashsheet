# smashsheet

Copies recent Smashrun runs into a running-log Google Sheet. Run it by hand, or
from cron every few hours.

For each Smashrun run that started on or after `max(start_date, now - lookback_hours)`:
if no logged row (one with Run time filled in) has that date, it fills the next
blank row:

- **Date** as `=<date col><previous row>+<days>`, so a column of pre-filled
  future dates chained off the row above keeps working.
- **Run time** (`h:mm:ss`) and **distance in km**.
- Any columns listed in `copy_from_previous_row` (e.g. a shoe key).
- Any `row_formulas` (`{r}` is replaced with the row number).
- If `standard_run` is enabled and the run looks like it: its `set_columns`.

It never inserts a run older than the last logged row into the middle of the sheet.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json   # config.json is gitignored; fill it in
```

### Smashrun token

Go to https://api.smashrun.com/v1/explorer, type `client` as the client id, click
connect, and paste the access token into `smashrun.access_token`. These
user-level tokens can't be refreshed and need redoing roughly every 60 days.
When one expires, the script exits with status 2 and logs how to fix it. If you
get a registered client from Smashrun (hi@smashrun.com), fill in
`client_id` / `client_secret` / `refresh_token` and the script refreshes on its own.

Smashrun calls are rate-limited (default: one call per 5 s, at most 20 per
10 min, tracked across runs in `.smashrun_calls.json`). Smashrun's documented
limit for user tokens is 250/hour, so this stays well under it. Tune it in
`smashrun.rate_limit`.

### Google service account

1. In the [Google Cloud Console](https://console.cloud.google.com/), pick or
   create a project and enable the **Google Sheets API**
   (APIs & Services → Library → "Google Sheets API" → Enable).
2. APIs & Services → Credentials → Create credentials → **Service account**.
   Open it → Keys → Add key → JSON. Save the file as `service-account.json` in
   this directory (gitignored).
3. Copy the service account's email (`...@<project>.iam.gserviceaccount.com`)
   and **share your sheet with it as Editor**.

### Spreadsheet ID and sheet name

Open the spreadsheet in a browser. The URL looks like

```
https://docs.google.com/spreadsheets/d/1AbC...xYz/edit#gid=0
```

The **spreadsheet ID** is the long string between `/d/` and the next `/`
(`1AbC...xYz` above). Put it in `google.spreadsheet_id`.

`google.sheet_name` is the name on the **tab** at the bottom of the
spreadsheet that holds one row per run (not the spreadsheet's title). Row 1 is
treated as a header.

### Sheet layout

`sheet_layout.columns` maps `date`, `run_time` and `km` to column letters.
Columns you compute in the sheet (pace, miles, links) go in `row_formulas`, or
you can leave them out if your blank rows already have the formulas.

### Treadmill (optional)

With `treadmill.enabled` set to `true`, any run Smashrun flags as a treadmill
run gets `treadmill.set_columns` written (e.g. `{"H": "T"}`). That takes
priority over the standard-run check.

### Standard run (optional)

To tag a regular route, set `standard_run.enabled` to `true`. A run matches when
its distance is between `min_km` and `max_km` and, if `start_lat`/`start_lon` are
set, its first GPS point is within `start_radius_m` of that point. Runs without
GPS never match once a start point is set. On a match, `set_columns` gets written
(e.g. `{"H": "A"}`).

## Running

```sh
.venv/bin/python smashsheet.py --dry-run -v   # show what it would add
.venv/bin/python smashsheet.py                # do it
./run.sh                                      # same, appending to smashsheet.log
```
