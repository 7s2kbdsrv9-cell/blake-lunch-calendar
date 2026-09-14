import calendar
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup


MENU_PAGE = "https://www.blakeschool.org/quicklinks/whats-for-lunch"

CALENDARS = {
    "middle-school": {
        "pdf_name": "lunch_ms.pdf",
        "label": "MS",
        "calendar_name": "Blake Middle School Lunch",
        "output": "middle-school.ics",
    },
    "upper-elementary": {
        "pdf_name": "lunch_ue.pdf",
        "label": "UE",
        "calendar_name": "Blake Upper Elementary Lunch",
        "output": "upper-elementary.ics",
    },
}

MONTH_NAMES = (
    "January February March April May June July August "
    "September October November December"
).split()

HEADERS = {
    "User-Agent": "Blake-Lunch-Calendar/1.0",
}


def download(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.content


def find_menu_urls():
    html = download(MENU_PAGE).decode("utf-8", errors="replace")

    # Finalsite sometimes stores links inside page data rather than normal
    # HTML anchors. Decode the common escaped forms before searching.
    searchable = (
        html.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("&amp;", "&")
    )

    soup = BeautifulSoup(searchable, "html.parser")
    found = {}

    for key, config in CALENDARS.items():
        filename = config["pdf_name"]

        # First try ordinary links.
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if filename in href:
                found[key] = urljoin(MENU_PAGE, href)
                break

        # Then search page data and scripts for the PDF URL.
        if key not in found:
            pattern = rf"""(?P<url>
                https?://[^"'<>\\s]+{re.escape(filename)}
                |
                /[^"'<>\\s]*{re.escape(filename)}
            )"""

            match = re.search(
                pattern,
                searchable,
                flags=re.IGNORECASE | re.VERBOSE,
            )

            if match:
                found[key] = urljoin(MENU_PAGE, match.group("url"))

        if key not in found:
            raise RuntimeError(
                f"Could not find {filename} on Blake's lunch page."
            )

    return found

    for key, config in CALENDARS.items():
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if config["pdf_name"] in href:
                found[key] = urljoin(MENU_PAGE, href)
                break

        if key not in found:
            raise RuntimeError(
                f"Could not find {config['pdf_name']} on Blake's lunch page."
            )

    return found


def detect_month_and_year(page):
    text = page.extract_text() or ""
    month_pattern = "|".join(MONTH_NAMES)
    match = re.search(
        rf"\b({month_pattern})\s+((?:20)\d{{2}})\b",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        raise RuntimeError("Could not identify the menu month and year.")

    month = MONTH_NAMES.index(match.group(1).title()) + 1
    year = int(match.group(2))
    return year, month


def extract_calendar_table(page):
    settings = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "intersection_tolerance": 8,
        "snap_tolerance": 5,
        "join_tolerance": 5,
    }

    candidates = page.extract_tables(settings)

    for table in candidates:
        if not table or len(table) < 2:
            continue

        first_row = " ".join(str(cell or "") for cell in table[0]).upper()

        if (
            len(table[0]) >= 5
            and "MONDAY" in first_row
            and "FRIDAY" in first_row
        ):
            return [row[:5] for row in table]

    raise RuntimeError("Could not reliably locate the calendar grid.")


def clean_cell(cell):
    if not cell:
        return []

    lines = []

    for raw_line in str(cell).splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            lines.append(line)

    if not lines:
        return []

    # Remove the date number printed at the beginning of each calendar cell.
    lines[0] = re.sub(r"^\d{1,2}\s*", "", lines[0]).strip()

    if lines and not lines[0]:
        lines.pop(0)

    return lines


def make_summary(label, lines):
    first = lines[0]
    upper = " ".join(lines).upper()

    if "NO SCHOOL" in upper:
        reason = " ".join(lines)
        return f"{label}: {reason.title()}"

    if "EARLY DISMISSAL" in upper:
        reason = " ".join(lines)
        return f"{label}: {reason.title()}"

    return f"{label} Lunch: {first}"


def extract_events(pdf_bytes, label):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            raise RuntimeError("The downloaded PDF has no pages.")

        page = pdf.pages[0]
        year, month = detect_month_and_year(page)
        table = extract_calendar_table(page)

    weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdayscalendar(
        year, month
    )

    data_rows = table[1:]
    events = []

    for row_index, week in enumerate(weeks):
        if row_index >= len(data_rows):
            break

        row = data_rows[row_index]

        for column in range(5):
            day = week[column]

            if not day:
                continue

            cell = row[column] if column < len(row) else None
            lines = clean_cell(cell)

            if not lines:
                continue

            event_date = date(year, month, day)

            events.append(
                {
                    "date": event_date,
                    "summary": make_summary(label, lines),
                    "description": "\n".join(lines),
                }
            )

    menu_days = [
        event for event in events if "LUNCH:" in event["summary"].upper()
    ]

    if len(menu_days) < 10:
        raise RuntimeError(
            f"Safety check failed: only {len(menu_days)} lunch days were found."
        )

    return year, month, events


def escape_ics(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def fold_line(line, limit=73):
    encoded = line.encode("utf-8")

    if len(encoded) <= limit:
        return line

    output = []
    current = ""

    for character in line:
        proposed = current + character

        if len(proposed.encode("utf-8")) > limit:
            output.append(current)
            current = " " + character
        else:
            current = proposed

    if current:
        output.append(current)

    return "\r\n".join(output)


def event_block(event, calendar_key):
    start = event["date"]
    end = start + timedelta(days=1)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"blake-{calendar_key}-{start:%Y%m%d}@github"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end:%Y%m%d}",
        f"SUMMARY:{escape_ics(event['summary'])}",
        f"DESCRIPTION:{escape_ics(event['description'])}",
        "LOCATION:The Blake School",
        "END:VEVENT",
    ]

    return "\r\n".join(fold_line(line) for line in lines)


def existing_event_blocks(path, replaced_year, replaced_month):
    if not path.exists():
        return []

    contents = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.findall(
        r"BEGIN:VEVENT.*?END:VEVENT",
        contents,
        flags=re.DOTALL,
    )

    cutoff = date.today() - timedelta(days=120)
    retained = []

    for block in blocks:
        match = re.search(
            r"DTSTART;VALUE=DATE:(\d{8})",
            block,
        )

        if not match:
            continue

        event_date = datetime.strptime(match.group(1), "%Y%m%d").date()

        if event_date < cutoff:
            continue

        if (
            event_date.year == replaced_year
            and event_date.month == replaced_month
        ):
            continue

        retained.append((event_date, block.replace("\n", "\r\n")))

    return retained


def write_calendar(config, calendar_key, year, month, new_events):
    path = Path(config["output"])
    combined = existing_event_blocks(path, year, month)

    for event in new_events:
        combined.append(
            (
                event["date"],
                event_block(event, calendar_key),
            )
        )

    combined.sort(key=lambda item: item[0])

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Blake Lunch Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_ics(config['calendar_name'])}",
    ]

    text = "\r\n".join(lines)
    text += "\r\n"

    for _, block in combined:
        text += block.strip() + "\r\n"

    text += "END:VCALENDAR\r\n"
    path.write_bytes(text.encode("utf-8"))


def main():
    urls = find_menu_urls()
    source_record = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "menu_page": MENU_PAGE,
        "calendars": {},
    }

    for key, config in CALENDARS.items():
        url = urls[key]
        pdf_bytes = download(url)
        year, month, events = extract_events(pdf_bytes, config["label"])

        write_calendar(config, key, year, month, events)

        source_record["calendars"][key] = {
            "pdf_url": url,
            "month": f"{year:04d}-{month:02d}",
            "events_found": len(events),
        }

        print(
            f"Updated {config['output']} with "
            f"{len(events)} events for {year}-{month:02d}."
        )

    Path("menu-sources.json").write_text(
        json.dumps(source_record, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
