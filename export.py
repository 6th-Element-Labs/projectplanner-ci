"""On-demand exports from LIVE task state (not static files): Excel (.xlsx) and
Microsoft Project (MSPDI .xml). Ported from build_plan_artifacts.py to read the
store's current rows so exports always reflect edits."""
import datetime
import io
import xml.sax.saxutils as sx
from typing import Any, Dict, List, Tuple

HOURS_PER_DAY = 8


def _ordered(payload: Dict[str, Any]) -> List[Tuple[str, str, Dict[str, Any]]]:
    rows = []
    for w in payload.get("workstreams", []):
        for t in w["tasks"]:
            rows.append((w["workstream_id"], w["name"], t))
    rows.sort(key=lambda x: (x[0], x[2].get("start_day") or 0))
    return rows


def export_xlsx(payload: Dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Project Plan"
    headers = ["Workstream", "Task ID", "Task", "Owner Org", "Owner", "Assignee", "Phase",
               "Status", "Start", "Finish", "Duration (d)", "Effort (d)", "Depends On",
               "Risk", "Blocking", "Description"]
    ws.append(headers)
    fill = PatternFill("solid", fgColor="1E3A5F")
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(vertical="center")
    for wsid, _wsname, t in _ordered(payload):
        def _d(v):
            try:
                return datetime.date.fromisoformat(v) if v else None
            except Exception:
                return None
        ws.append([wsid, t["task_id"], t.get("title"), t.get("owner_org"),
                   t.get("owner_person_or_role"), t.get("assignee"), t.get("phase"),
                   t.get("status"), _d(t.get("start_date")), _d(t.get("finish_date")),
                   t.get("duration_days"), t.get("effort_days"),
                   ", ".join(t.get("depends_on", [])), t.get("risk_level"),
                   "Yes" if t.get("is_blocking") else "", t.get("description")])
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=9).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=10).number_format = "yyyy-mm-dd"
    for i, wdt in enumerate([12, 10, 44, 14, 20, 16, 11, 13, 12, 12, 11, 10, 18, 9, 9, 70], 1):
        ws.column_dimensions[get_column_letter(i)].width = wdt
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_mspdi(payload: Dict[str, Any]) -> str:
    def esc(s):
        return sx.escape(str(s if s is not None else ""))

    def dt(d, hh="08:00:00"):
        return f"{d}T{hh}" if d else ""

    start = payload.get("schedule_start") or datetime.date.today().isoformat()
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<Project xmlns="http://schemas.microsoft.com/project">',
             '<Name>Project Maxwell.xml</Name>',
             f'<Title>{esc(payload.get("project"))}</Title>',
             f'<StartDate>{dt(start)}</StartDate>',
             '<CalendarUID>1</CalendarUID><ScheduleFromStart>1</ScheduleFromStart>',
             '<DefaultStartTime>08:00:00</DefaultStartTime><DefaultFinishTime>17:00:00</DefaultFinishTime>',
             '<MinutesPerDay>480</MinutesPerDay><MinutesPerWeek>2400</MinutesPerWeek>',
             '<DaysPerMonth>20</DaysPerMonth><DurationFormat>7</DurationFormat>',
             '<Calendars><Calendar><UID>1</UID><Name>Standard</Name><IsBaseCalendar>1</IsBaseCalendar><WeekDays>']
    for day in range(1, 8):
        if 2 <= day <= 6:
            lines.append(f'<WeekDay><DayType>{day}</DayType><DayWorking>1</DayWorking><WorkingTimes>'
                         '<WorkingTime><FromTime>08:00:00</FromTime><ToTime>12:00:00</ToTime></WorkingTime>'
                         '<WorkingTime><FromTime>13:00:00</FromTime><ToTime>17:00:00</ToTime></WorkingTime>'
                         '</WorkingTimes></WeekDay>')
        else:
            lines.append(f'<WeekDay><DayType>{day}</DayType><DayWorking>0</DayWorking></WeekDay>')
    lines.append('</WeekDays></Calendar></Calendars><Tasks>')
    lines.append(f'<Task><UID>0</UID><ID>0</ID><Name>{esc(payload.get("project"))}</Name>'
                 '<OutlineLevel>0</OutlineLevel><Summary>1</Summary></Task>')
    uid, tid, uid_by_task = 1, 1, {}
    for w in payload.get("workstreams", []):
        wt = sorted(w["tasks"], key=lambda t: t.get("start_day") or 0)
        if not wt:
            continue
        wstart = min(t.get("start_date") or start for t in wt)
        wfin = max(t.get("finish_date") or start for t in wt)
        lines.append(f'<Task><UID>{uid}</UID><ID>{tid}</ID>'
                     f'<Name>{esc(w["workstream_id"] + " — " + w["name"])}</Name>'
                     f'<OutlineLevel>1</OutlineLevel><Summary>1</Summary>'
                     f'<Start>{dt(wstart)}</Start><Finish>{dt(wfin, "17:00:00")}</Finish></Task>')
        uid += 1; tid += 1
        for t in wt:
            uid_by_task[t["task_id"]] = uid
            dur = t.get("duration_days") or 1
            lines.append(
                f'<Task><UID>{uid}</UID><ID>{tid}</ID><Name>{esc(t["task_id"] + " " + (t.get("title") or ""))}</Name>'
                f'<OutlineLevel>2</OutlineLevel><Manual>1</Manual>'
                f'<Start>{dt(t.get("start_date") or start)}</Start>'
                f'<Finish>{dt(t.get("finish_date") or start, "17:00:00")}</Finish>'
                f'<Duration>PT{dur * HOURS_PER_DAY}H0M0S</Duration><DurationFormat>7</DurationFormat>'
                f'<Notes>{esc(t.get("description"))}</Notes>'
                + "".join(f'<PredecessorLink><PredecessorUID>{uid_by_task[d]}</PredecessorUID><Type>1</Type></PredecessorLink>'
                          for d in t.get("depends_on", []) if d in uid_by_task)
                + '</Task>')
            uid += 1; tid += 1
    lines.append('</Tasks></Project>')
    return "\n".join(lines)
