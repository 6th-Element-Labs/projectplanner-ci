# Sierra XLSX Analysis

**File:** `Taikun Data Collection-Sensirion Data.xlsx`
**Sheets (5):** ['Sensirion Alert Data', 'daily-notes_export_01-28-26 (2)', ' Sensirion Data', 'Pad List of Dates wo Alerts', 'Relevant LO Notes']

---

> **Clarification on "NEW" tag:** Below, columns flagged `**NEW**` mean Sierra uses a different *header string* in her xlsx (more verbose) than our `emissions.alerts` field name. They are **not new database columns**. The underlying *data* maps 1:1 to existing schema fields:
>
> - `Email Actually Received Date and Time` → `email_received`
> - `Was the Alert Cleared In Office or In Field?` → `cleared_location`
> - `Date Emissions Alert Resolution Email was Sent` → `resolution_date`
> - `How was the Alert Cleared:` → `how_cleared`
> - `Number of Thief Hatches Open/Repaired/Replaced` → `thief_hatches_open/_repaired/_replaced`
> - `User`, `Date`, `Wells`, `Compressors`, `Note`, `Department` (daily_notes tab) → `username`, `note_date`, `wells`, `compressors`, `note`, `department`
> - `Sensirion Alert ID: (column E)` (linked notes tab) → `emission_id`
> - `Dates: ` (pad baselines tab) → `clean_date_range`
>
> **All 22 alert columns and all daily-notes/linked-notes/pad-baselines columns map to existing `emissions.alerts.*` schema. No schema changes needed.** The Excel export just needs to use Sierra's verbose headers when rendering.


## Sheet: `Sensirion Alert Data`

**Rows:** 179 · **Cols:** 22

**Columns** (with schema match check):

| # | Column header | Normalized | In schema? | Sample value |
|---|---|---|---|---|
| 1 | `Status` | `status` | ✓ | Closed |
| 2 | `Pad` | `pad` | ✓ | 4th Street North Pad |
| 3 | `Pad Code` | `pad_code` | ✓ | 906003 |
| 4 | `Route` | `route` | ✓ | Route_07 |
| 5 | `Emission ID` | `emission_id` | ✓ | 0ca2f68d-606b-4548-92f2-f4bbed83d1e7 |
| 6 | `Emissions Rate per Email Notification (kg/h)` | `emissions_rate_per_email_notification_kg_h` | ✓ | 21.77 |
| 7 | `Emission Start Date & Time: ` | `emission_start_date_&_time:` | ✓ | 2026-01-10 03:00:00 |
| 8 | `Email Actually Received Date and Time ` | `email_actually_received_date_and_time` | **NEW** | 2026-01-10 07:22:00 |
| 9 | `Was the Alert Cleared In Office or In Field? ` | `was_the_alert_cleared_in_office_or_in_field?` | **NEW** | Office |
| 10 | `Date Emissions Alert Resolution Email was Sent: ` | `date_emissions_alert_resolution_email_was_sent:` | **NEW** | 2026-01-20 08:04:00 |
| 11 | `Resolution Personnel` | `resolution_personnel` | ✓ | Kolby Holster |
| 12 | `Problem Identified ` | `problem_identified` | ✓ | Dump Hung Open per LO Notes |
| 13 | `How was the Alert Cleared:` | `how_was_the_alert_cleared:` | **NEW** | The alert was cleared by viewing a drop in injection via Pro |
| 14 | `Resolution Type:` | `resolution_type:` | ✓ | Process Emissions |
| 15 | `Equipment: ` | `equipment:` | ✓ | Process Emissions |
| 16 | `Equipment Component: ` | `equipment_component:` | ✓ | Process Emissions |
| 17 | `EPA identifier` | `epa_identifier` | ✓ | Process Emissions |
| 18 | `Resolution` | `resolution` | ✓ | sent email to close out alert |
| 19 | `thief hatch` | `thief_hatch` | ✓ | no |
| 20 | `Number of Thief Hatches Open` | `number_of_thief_hatches_open` | **NEW** | 1.0 |
| 21 | `Number of Thief Hatches Repaired` | `number_of_thief_hatches_repaired` | **NEW** | 0.0 |
| 22 | `Number of Thief Hatches Replaced` | `number_of_thief_hatches_replaced` | **NEW** | 0.0 |

**First 2 rows (selected fields):**

- Row 1:
    - `Status`: Closed
    - `Pad`: 4th Street North Pad
    - `Pad Code`: 906003
    - `Route`: Route_07
    - `Emission ID`: 0ca2f68d-606b-4548-92f2-f4bbed83d1e7
    - `Emissions Rate per Email Notification (kg/h)`: 21.77
    - `Emission Start Date & Time: `: 2026-01-10 03:00:00
    - `Email Actually Received Date and Time `: 2026-01-10 07:22:00
    - `Was the Alert Cleared In Office or In Field? `: Office
    - `Date Emissions Alert Resolution Email was Sent: `: 2026-01-20 08:04:00
    - `Resolution Personnel`: Kolby Holster
    - `Problem Identified `: Dump Hung Open per LO Notes
    - `How was the Alert Cleared:`: The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop in sales rates via Cygnet, a casing pres
    - `Resolution Type:`: Process Emissions
    - `Equipment: `: Process Emissions
    - `Equipment Component: `: Process Emissions
    - `EPA identifier`: Process Emissions
    - `Resolution`: sent email to close out alert
    - `thief hatch`: no

- Row 2:
    - `Status`: Closed
    - `Pad`: 4th Street North Pad
    - `Pad Code`: 906003
    - `Route`: Route_07
    - `Emission ID`: 562ed1fa-2cf9-4e3d-9998-82f843885c5b
    - `Emissions Rate per Email Notification (kg/h)`: 39.92
    - `Emission Start Date & Time: `: 2026-01-17 05:00:00
    - `Email Actually Received Date and Time `: 2026-01-17 07:38:00
    - `Was the Alert Cleared In Office or In Field? `: Office
    - `Date Emissions Alert Resolution Email was Sent: `: 2026-01-20 08:04:00
    - `Resolution Personnel`: Kolby Holster
    - `Problem Identified `: Dump Hung Open per LO Notes
    - `How was the Alert Cleared:`: The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop in sales rates via Cygnet, a casing pres
    - `Resolution Type:`: Process Emissions
    - `Equipment: `: Process Emissions
    - `Equipment Component: `: Process Emissions
    - `EPA identifier`: Process Emissions
    - `Resolution`: sent email to close out alert
    - `thief hatch`: no

**Controlled vocabularies** (low-cardinality columns):

- **`Status`** (3 unique):
    - `Closed` × 166
    - `Closed (2nd Email)` × 12
    - ` ` × 1

- **`Was the Alert Cleared In Office or In Field? `** (2 unique):
    - `Office` × 104
    - `Field` × 75

- **`Resolution Personnel`** (5 unique):
    - `Kolby Holster` × 67
    - `Billy Thomson` × 38
    - `Lance Skakun` × 37
    - `Kaleb Webb` × 32
    - `Devin Rushing` × 5

- **`How was the Alert Cleared:`** (14 unique):
    - `The alerts was cleared with a visit to the field. ` × 75
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet a` × 41
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 34
    - `The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop ` × 11
    - `The alert was cleared by viewing the compressor metrics in Cygnet, a drop in Inj` × 7
    - `The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop ` × 3
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1
    - `The alert was cleared by viewing the compressor metrics in Cygnet, a drop in Inj` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet a` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1
    - `The alert was cleared by viewing a liquids unloading event within Cygnet. ` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1

- **`Resolution Type:`** (4 unique):
    - `Process Emissions` × 126
    - `Unexpected` × 47
    - `Undetected` × 4
    - `Process Emissions ` × 2

- **`Equipment: `** (8 unique):
    - `Process Emissions` × 127
    - `Tank` × 25
    - `Compressor Scrubbers` × 16
    - `Compressor Cooler` × 2
    - `Pipeline- Third Party` × 2
    - `Process Emissions ` × 1
    - `Separator` × 1
    - `Wellhead` × 1

- **`Equipment Component: `** (10 unique):
    - `Process Emissions` × 127
    - `Thief Hatch` × 23
    - `Dump Valve Controler` × 15
    - `PRV` × 2
    - `PLM` × 2
    - `Body` × 2
    - `Process Emissions ` × 1
    - `Gauge` × 1
    - `Q exhaust` × 1
    - `Casing Wing Valve` × 1

- **`EPA identifier`** (7 unique):
    - `Process Emissions` × 127
    - `PRV` × 25
    - `Valve-C` × 15
    - `Other` × 5
    - `Process Emissions ` × 1
    - `Open Ended Line` × 1
    - `Valve` × 1

- **`Resolution`** (2 unique):
    - `sent email to close out alert` × 176
    - `did not send email out` × 2

- **`thief hatch`** (2 unique):
    - `no` × 154
    - `yes` × 24

- **`Number of Thief Hatches Open`** (6 unique):
    - `1.0` × 8
    - `2.0` × 5
    - `0.0` × 4
    - `3.0` × 3
    - `5.0` × 3
    - `7.0` × 1

- **`Number of Thief Hatches Repaired`** (5 unique):
    - `0.0` × 19
    - `1.0` × 2
    - `2.0` × 1
    - `3.0` × 1
    - `7.0` × 1

- **`Number of Thief Hatches Replaced`** (3 unique):
    - `0.0` × 20
    - `1.0` × 2
    - `3.0` × 2


---

## Sheet: `daily-notes_export_01-28-26 (2)`

**Rows:** 1677 · **Cols:** 9

**Columns** (with schema match check):

| # | Column header | Normalized | In schema? | Sample value |
|---|---|---|---|---|
| 1 | `Unnamed: 0` | `unnamed:_0` | **NEW** |  |
| 2 | `User` | `user` | **NEW** | anthony.barricelli |
| 3 | `Date` | `date` | **NEW** | 1/28/26, 2:13 PM |
| 4 | `Route` | `route` | ✓ | Route_19 |
| 5 | `Pad` | `pad` | ✓ | ROCK CREEK C PAD |
| 6 | `Wells` | `wells` | **NEW** | ROCK CREEK C 1H,ROCK CREEK C 2H,ROCK CREEK C 4H,ROCK CREEK C |
| 7 | `Compressors` | `compressors` | **NEW** | MC2795,MC3077,MC3772,MC4074 |
| 8 | `Note` | `note` | **NEW** | Pad is back online.  |
| 9 | `Department` | `department` | **NEW** | Compressors |

**First 2 rows (selected fields):**

- Row 1:
    - `User`: anthony.barricelli
    - `Date`: 1/28/26, 2:13 PM
    - `Route`: Route_19
    - `Pad`: ROCK CREEK C PAD
    - `Wells`: ROCK CREEK C 1H,ROCK CREEK C 2H,ROCK CREEK C 4H,ROCK CREEK C 5H,ROCK CREEK C 6H,ROCK CREEK C 7H,ROCK CREEK C 9H
    - `Note`: Pad is back online. 

- Row 2:
    - `User`: Gary.Huddleston
    - `Date`: 1/28/26, 2:01 PM
    - `Route`: Route_01
    - `Pad`: DEREK WEAVER PAD
    - `Wells`: LAKE WORTH A TRT 10H,LAKE WORTH A TRT 12H,LAKE WORTH A TRT 13H,LAKE WORTH A TRT 8H,LAKE WORTH A TRT 9H,LAKESIDE 1H,LAKES
    - `Compressors`: MC2795,MC3077,MC3772,MC4074
    - `Note`: Worked with Trever to bring remaining compressors and wells back on. Having issues with comp 3077 going down on Ch 10 EI

**Controlled vocabularies** (low-cardinality columns):

- **`Department`** (2 unique):
    - `Compressors` × 67
    - `Pad Air` × 46


---

## Sheet: ` Sensirion Data`

**Rows:** 50 · **Cols:** 22

**Columns** (with schema match check):

| # | Column header | Normalized | In schema? | Sample value |
|---|---|---|---|---|
| 1 | `Status` | `status` | ✓ | Closed |
| 2 | `Pad` | `pad` | ✓ | 4th Street North Pad |
| 3 | `Pad Code` | `pad_code` | ✓ | 906003 |
| 4 | `Route` | `route` | ✓ | Route_07 |
| 5 | `Emission ID` | `emission_id` | ✓ | 458fafbb-c269-430a-af88-eb3873342e72 |
| 6 | `Emissions Rate per Email Notification (kg/h)` | `emissions_rate_per_email_notification_kg_h` | ✓ | 39.46 |
| 7 | `Emission Start Date & Time: ` | `emission_start_date_&_time:` | ✓ | 2026-01-17 20:00:00 |
| 8 | `Email Actually Received Date and Time ` | `email_actually_received_date_and_time` | **NEW** | 2026-01-18 01:18:00 |
| 9 | `Was the Alert Cleared In Office or In Field? ` | `was_the_alert_cleared_in_office_or_in_field?` | **NEW** | Office |
| 10 | `Date Emissions Alert Resolution Email was Sent: ` | `date_emissions_alert_resolution_email_was_sent:` | **NEW** | 2026-01-20 08:04:00 |
| 11 | `MRO Resolution Personnel: ` | `mro_resolution_personnel:` | **NEW** | Kolby Holster |
| 12 | `Problem Identified via Email reply: ` | `problem_identified_via_email_reply:` | ✓ | Dump Hung Open per LO Notes |
| 13 | `How was the Alert Cleared:` | `how_was_the_alert_cleared:` | **NEW** | The alert was cleared by viewing a drop in injection via Pro |
| 14 | `Resolution Type:` | `resolution_type:` | ✓ | Process Emissions |
| 15 | `Equipment: ` | `equipment:` | ✓ | Process Emissions |
| 16 | `Equipment Component: ` | `equipment_component:` | ✓ | Process Emissions |
| 17 | `EPA identifier` | `epa_identifier` | ✓ | Process Emissions |
| 18 | `Resolution` | `resolution` | ✓ | sent email to close out alert |
| 19 | `thief hatch` | `thief_hatch` | ✓ | no |
| 20 | `Number of Thief Hatches Open` | `number_of_thief_hatches_open` | **NEW** | 1.0 |
| 21 | `Number of Thief Hatches Repaired` | `number_of_thief_hatches_repaired` | **NEW** | 0.0 |
| 22 | `Number of Thief Hatches Replaced` | `number_of_thief_hatches_replaced` | **NEW** | 0.0 |

**First 2 rows (selected fields):**

- Row 1:
    - `Status`: Closed
    - `Pad`: 4th Street North Pad
    - `Pad Code`: 906003
    - `Route`: Route_07
    - `Emission ID`: 458fafbb-c269-430a-af88-eb3873342e72
    - `Emissions Rate per Email Notification (kg/h)`: 39.46
    - `Emission Start Date & Time: `: 2026-01-17 20:00:00
    - `Email Actually Received Date and Time `: 2026-01-18 01:18:00
    - `Was the Alert Cleared In Office or In Field? `: Office
    - `Date Emissions Alert Resolution Email was Sent: `: 2026-01-20 08:04:00
    - `MRO Resolution Personnel: `: Kolby Holster
    - `Problem Identified via Email reply: `: Dump Hung Open per LO Notes
    - `How was the Alert Cleared:`: The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop in sales rates via Cygnet, a casing pres
    - `Resolution Type:`: Process Emissions
    - `Equipment: `: Process Emissions
    - `Equipment Component: `: Process Emissions
    - `EPA identifier`: Process Emissions
    - `Resolution`: sent email to close out alert
    - `thief hatch`: no

- Row 2:
    - `Status`: Closed
    - `Pad`: 4th Street North Pad
    - `Pad Code`: 906003
    - `Route`: Route_07
    - `Emission ID`: 4afda8c6-50e9-414e-b32d-17525661e1e6
    - `Emissions Rate per Email Notification (kg/h)`: 26.21
    - `Emission Start Date & Time: `: 2026-01-18 08:00:00
    - `Email Actually Received Date and Time `: 2026-01-18 13:14:00
    - `Was the Alert Cleared In Office or In Field? `: Office
    - `Date Emissions Alert Resolution Email was Sent: `: 2026-01-20 08:04:00
    - `MRO Resolution Personnel: `: Kolby Holster
    - `Problem Identified via Email reply: `: Dump Hung Open per LO Notes
    - `How was the Alert Cleared:`: The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop in sales rates via Cygnet, a casing pres
    - `Resolution Type:`: Process Emissions
    - `Equipment: `: Process Emissions
    - `Equipment Component: `: Process Emissions
    - `EPA identifier`: Process Emissions
    - `Resolution`: sent email to close out alert
    - `thief hatch`: no

**Controlled vocabularies** (low-cardinality columns):

- **`Status`** (2 unique):
    - `Closed` × 48
    - `Closed (2nd Email)` × 2

- **`Route`** (16 unique):
    - `Route_07` × 11
    - `Route_31` × 5
    - `Route_05` × 5
    - `Route_06` × 4
    - `Route_13` × 3
    - `Route_10` × 3
    - `Route_30` × 3
    - `Route_32` × 3
    - `Route_21` × 2
    - `Route_16` × 2
    - `Route_18` × 2
    - `Route_23` × 2
    - `Route_09` × 2
    - `Route_19` × 1
    - `Route_01` × 1

- **`Was the Alert Cleared In Office or In Field? `** (2 unique):
    - `Office` × 38
    - `Field` × 12

- **`MRO Resolution Personnel: `** (5 unique):
    - `Kolby Holster` × 24
    - `Kaleb Webb` × 11
    - `Billy Thomson` × 7
    - `Lance Skakun` × 7
    - `Devin Rushing` × 1

- **`How was the Alert Cleared:`** (11 unique):
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet a` × 17
    - `The alerts was cleared with a visit to the field. ` × 11
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 10
    - `The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop ` × 4
    - `The alert was cleared by viewing the compressor metrics in Cygnet, a drop in Inj` × 2
    - `The alerts was cleared with a visit to the field. Using Cygnet this alert was cl` × 1
    - `The alert was cleared by viewing a drop in injection via ProCount/Carte, a drop ` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet a` × 1
    - `The alert was cleared by viewing a drop in tubing and line pressure via Cygnet, ` × 1

- **`Resolution Type:`** (3 unique):
    - `Process Emissions` × 33
    - `Unexpected` × 9
    - `Process Emissions ` × 2

- **`Equipment: `** (4 unique):
    - `Process Emissions` × 34
    - `Tank` × 6
    - `Compressor Scrubbers` × 3
    - `Process Emissions ` × 1

- **`Equipment Component: `** (4 unique):
    - `Process Emissions` × 34
    - `Thief Hatch` × 6
    - `Dump Valve Controler` × 3
    - `Process Emissions ` × 1

- **`EPA identifier`** (4 unique):
    - `Process Emissions` × 34
    - `PRV` × 6
    - `Valve-C` × 3
    - `Process Emissions ` × 1

- **`Resolution`** (2 unique):
    - `sent email to close out alert` × 42
    - `did not send email out` × 1

- **`thief hatch`** (2 unique):
    - `no` × 37
    - `yes` × 6

- **`Number of Thief Hatches Open`** (2 unique):
    - `1.0` × 5
    - `3.0` × 1

- **`Number of Thief Hatches Repaired`** (1 unique):
    - `0.0` × 6

- **`Number of Thief Hatches Replaced`** (1 unique):
    - `0.0` × 6


---

## Sheet: `Pad List of Dates wo Alerts`

**Rows:** 32 · **Cols:** 4

**Columns** (with schema match check):

| # | Column header | Normalized | In schema? | Sample value |
|---|---|---|---|---|
| 1 | `Pad` | `pad` | ✓ | 4th Street North Pad |
| 2 | `Pad Code` | `pad_code` | ✓ | 906003 |
| 3 | `Route` | `route` | ✓ | Route_07 |
| 4 | `Date(s): ` | `dates:` | **NEW** | 2026-01-19 00:00:00 |

**First 2 rows (selected fields):**

- Row 1:
    - `Pad`: 4th Street North Pad
    - `Pad Code`: 906003
    - `Route`: Route_07
    - `Date(s): `: 2026-01-19 00:00:00

- Row 2:
    - `Pad`: 4th Street North Pad
    - `Pad Code`: 906004
    - `Route`: Route_08
    - `Date(s): `: 2026-01-22 00:00:00

**Controlled vocabularies** (low-cardinality columns):

- **`Route`** (16 unique):
    - `Route_07` × 5
    - `Route_16` × 3
    - `Route_10` × 3
    - `Route_08` × 2
    - `Route_06` × 2
    - `Route_18` × 2
    - `Route_31` × 2
    - `Route_30` × 2
    - `Route_09` × 2
    - `Route_05` × 2
    - `Route_32` × 2
    - `Route_13` × 1
    - `Route_21` × 1
    - `Route_19` × 1
    - `Route_23` × 1


---

## Sheet: `Relevant LO Notes`

**Rows:** 64 · **Cols:** 9

**Columns** (with schema match check):

| # | Column header | Normalized | In schema? | Sample value |
|---|---|---|---|---|
| 1 | `Sensirion Alert ID: (column E)` | `sensirion_alert_id:_column_e` | **NEW** | 3a037c7f-e625-4be1-8656-2f59f226b23c |
| 2 | `User` | `user` | **NEW** | dillon.graham |
| 3 | `Date` | `date` | **NEW** | 1/21/26, 6:57 AM |
| 4 | `Route` | `route` | ✓ | Route_07 |
| 5 | `Pad` | `pad` | ✓ | 4TH STREET NORTH PAD |
| 6 | `Wells` | `wells` | **NEW** | TINDALL 2H |
| 7 | `Compressors` | `compressors` | **NEW** | MC2994 |
| 8 | `Note` | `note` | **NEW** | Plunger check/ Mic'd at a 1.889, regular wear. Dropped a new |
| 9 | `Department` | `department` | **NEW** | Pad Air |

**First 2 rows (selected fields):**

- Row 1:
    - `Sensirion Alert ID: (column E)`: 3a037c7f-e625-4be1-8656-2f59f226b23c
    - `User`: dillon.graham
    - `Date`: 1/21/26, 6:57 AM
    - `Route`: Route_07
    - `Pad`: 4TH STREET NORTH PAD
    - `Wells`: TINDALL 2H
    - `Note`: Plunger check/ Mic'd at a 1.889, regular wear. Dropped a new plunger in the well and sat wit hit for a run. Plunger is t

- Row 2:
    - `Sensirion Alert ID: (column E)`: 3a037c7f-e625-4be1-8656-2f59f226b23c
    - `User`: dillon.graham
    - `Date`: 1/20/26, 3:01 PM
    - `Route`: Route_07
    - `Pad`: 4TH STREET NORTH PAD
    - `Wells`: TRWD BEND 4H
    - `Note`: Put kidney back into service./

**Controlled vocabularies** (low-cardinality columns):

- **`Route`** (16 unique):
    - `Route_05` × 13
    - `Route_07` × 11
    - `Route_31` × 7
    - `Route_10` × 5
    - `Route_06` × 4
    - `Route_13` × 3
    - `Route_30` × 3
    - `Route_23` × 3
    - `Route_09` × 3
    - `Route_32` × 3
    - `Route_21` × 2
    - `Route_16` × 2
    - `Route_18` × 2
    - `Route_19` × 1
    - `Route_01` × 1

- **`Compressors`** (13 unique):
    - `MC3972` × 3
    - `MC3989` × 3
    - `MC1415` × 3
    - `MC5072` × 2
    - `MC3713` × 2
    - `MC2994` × 1
    - `MC2637` × 1
    - `MC2508` × 1
    - `MC3772` × 1
    - `MC2669` × 1
    - `MC2730` × 1
    - `MC2951,MC3600` × 1
    - `MC3898` × 1


---

